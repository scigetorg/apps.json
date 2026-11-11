"""Backfill catalog license lists onto legacy records and selected mismatches.

neurocommand's old pipeline (deposit API) gave each record one license, often a
placeholder: an `other-*` category, Zenodo's default `cc-by-4.0` when none was
sent, or just the first of several. This edits the audit's `verified_legacy`
records, plus `mismatch` records with `--include-mismatch`, so their `rights`
are apps.json's licenses (recipe history). Nothing else changes: same record,
DOI, version and files. Dry run by default; `--apply` writes.

    audit_rows(apps.json, /api/user/records) ─► status verified_legacy
        ─► POST   /records/{id}/draft                 (open an edit draft)
        ─► PUT    /records/{id}/draft                 (rights = rights_from_catalog)
        ─► POST   /records/{id}/draft/actions/publish (same DOI)
        any error before publish ─► DELETE the edit draft (published record unchanged)

Modes:
    (default)            verified_legacy records only
    --include-mismatch   also mismatch records (Zenodo has multi-license that
                         differs from apps.json, e.g. after a recipe correction)
    --clear-placeholders no_catalog_license records whose Zenodo license is a
                         known placeholder (other-*, cc-by-4.0 default); clears
                         rights to [] since no real license is known
"""

import argparse
import os
import sys
from pathlib import Path

import requests

from .audit_doi_licenses import audit_rows
from .doi_license import (
    fetch_catalogue, license_equivalence_key, rights_from_catalog, same_rights,
)
from .license_terms import OTHER_IDS
from .zenodo_catalog import (
    RDM_JSON, ZENODO_API, build_session, catalog_artifacts, raise_draft_errors,
    record_doi, user_records,
)

# Fields the records API accepts back on a draft update.
EDITABLE = ("metadata", "files", "access", "pids", "custom_fields")

# Zenodo's default and the other-* placeholders: the old pipeline wrote these
# when it had no license, so they carry no signal about the actual terms.
_PLACEHOLDER_KEYS = (
    {("id", license_equivalence_key(other)) for other in OTHER_IDS}
    | {("id", license_equivalence_key("cc-by-4.0"))}
)


def legacy_targets(artifacts, records, titles=None, include_mismatch=False):
    statuses = {"verified_legacy"}
    if include_mismatch:
        statuses.add("mismatch")
    return [row for row in audit_rows(artifacts, records)
            if row["status"] in statuses and (not titles or row["title"] in titles)]


def placeholder_targets(artifacts, records, titles=None):
    """no_catalog_license records whose single Zenodo license is a known placeholder."""
    from .doi_license import rights_key
    targets = []
    for row in audit_rows(artifacts, records):
        if row["status"] != "no_catalog_license":
            continue
        if titles and row["title"] not in titles:
            continue
        published = (records.get(row["title"], {}).get("metadata") or {}).get("rights") or []
        pub_key = rights_key(published)
        if len(pub_key) == 1 and pub_key <= _PLACEHOLDER_KEYS:
            row["reason"] = "clearing placeholder (no license known)"
            targets.append(row)
    return targets


def raise_for_zenodo(response):
    """raise_for_status, keeping Zenodo's own explanation (e.g. "Invalid value gpl-2.0.")."""
    if response.status_code >= 400:
        try:
            detail = response.json()
            detail = detail.get("message") or detail.get("errors") or detail
        except ValueError:
            detail = response.text[:300]
        raise requests.HTTPError(f"HTTP {response.status_code} from Zenodo: {detail}", response=response)


def unknown_ids(rights, catalogue):
    """License IDs Zenodo's live catalogue does not know (they would be rejected)."""
    return sorted(right["id"] for right in rights if right.get("id") and right["id"] not in catalogue)


def replace_rights(session, record_id, rights):
    """Edit one published record so its rights are exactly `rights`; return it."""
    headers = {"Accept": RDM_JSON}
    draft_url = f"{ZENODO_API}/records/{record_id}/draft"
    opened = session.post(draft_url, headers=headers, timeout=(15, 60))
    raise_for_zenodo(opened)
    try:
        draft = opened.json()
        draft["metadata"]["rights"] = rights
        updated = session.put(draft_url, json={key: draft[key] for key in EDITABLE if key in draft},
                              headers=headers, timeout=(15, 60))
        raise_for_zenodo(updated)
        raise_draft_errors(updated.json())
        published = session.post(f"{draft_url}/actions/publish", headers=headers, timeout=(15, 120))
        raise_for_zenodo(published)
    except Exception:
        try:
            session.delete(draft_url, headers=headers, timeout=(15, 60))
        except requests.RequestException as error:
            print(f"Could not discard edit draft {record_id}: {error}", file=sys.stderr)
        raise
    return published.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog-root", type=Path, default=Path("."))
    parser.add_argument("--apply", action="store_true", help="write to Zenodo (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="at most this many records (0 = all)")
    parser.add_argument("--include-mismatch", action="store_true",
                        help="also update mismatch records (default: only verified_legacy)")
    parser.add_argument("--clear-placeholders", action="store_true",
                        help="clear placeholder licenses from no_catalog_license records")
    parser.add_argument("--title", action="append", default=[],
                        help="only this image title; repeat for several")
    args = parser.parse_args()
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        parser.error("ZENODO_TOKEN is required")
    if args.limit < 0:
        parser.error("--limit must be 0 or more")
    session = build_session(token)
    artifacts = catalog_artifacts(args.catalog_root)
    records = user_records(session)
    targets = legacy_targets(artifacts, records, set(args.title), args.include_mismatch)
    if args.clear_placeholders:
        targets += placeholder_targets(artifacts, records, set(args.title))
    # Checked in the dry run too, so IDs Zenodo would reject show up before --apply.
    catalogue = fetch_catalogue()["entries"]
    total = len(targets)
    if args.limit:
        targets = targets[:args.limit]
    print(f"{ZENODO_API}: {total} record(s) to update, handling {len(targets)}"
          f"{'' if args.apply else ' (dry run, nothing is written)'}", flush=True)
    updated, failures = 0, 0
    for row in targets:
        is_placeholder_clear = row["status"] == "no_catalog_license"
        expected = [] if is_placeholder_clear else None
        target_label = "[]" if is_placeholder_clear else str(row.get("catalog_license"))
        line = (f"{row['title']} {row['doi_url']}: {row['published_license']} -> "
                f"{row.get('catalog_license', 'unavailable')} ({row['reason']})")
        try:
            if expected is None:
                expected = rights_from_catalog(artifacts[row["title"]])
            if unknown := unknown_ids(expected, catalogue):
                raise ValueError(f"not Zenodo license IDs: {unknown} (fix the recipe license)")
            if not args.apply:
                print(f"would update {line}", flush=True)
                continue
            record = replace_rights(session, row["id"], expected)
            raise_draft_errors(record)
            doi = record_doi(record)
            if f"https://doi.org/{doi}" != row["doi_url"]:
                raise ValueError(f"DOI changed to {doi}")
            if not same_rights((record.get("metadata") or {}).get("rights") or [], expected):
                raise ValueError("published rights differ from apps.json")
            updated += 1
            print(f"updated {line}", flush=True)
        except Exception as exc:
            failures += 1
            print(f"FAILED {line}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    if args.apply:
        print(f"updated {updated}, failed {failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
