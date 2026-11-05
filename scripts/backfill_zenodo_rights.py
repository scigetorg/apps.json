"""Backfill full license lists onto records published with one legacy license.

The legacy deposit API allowed one license per record, so images with several
or custom licenses were published as a single `other-open`/`other-closed`.
This edits exactly the audit's `verified_legacy` records so their `rights`
list every license from apps.json. Nothing else changes: same record, DOI,
version and files. Dry run by default; `--apply` writes.

    audit_rows(apps.json, /api/user/records) ─► status verified_legacy
        ─► POST   /records/{id}/draft                 (open an edit draft)
        ─► PUT    /records/{id}/draft                 (rights = rights_from_catalog)
        ─► POST   /records/{id}/draft/actions/publish (same DOI)
        any error before publish ─► DELETE the edit draft (published record unchanged)
"""

import argparse
import os
import sys
from pathlib import Path

import requests

from .audit_doi_licenses import audit_rows
from .doi_license import rights_from_catalog, same_rights
from .zenodo_catalog import (
    RDM_JSON, ZENODO_API, build_session, catalog_artifacts, raise_draft_errors,
    record_doi, user_records,
)

# Fields the records API accepts back on a draft update.
EDITABLE = ("metadata", "files", "access", "pids", "custom_fields")


def legacy_targets(artifacts, records, titles=None):
    return [row for row in audit_rows(artifacts, records)
            if row["status"] == "verified_legacy" and (not titles or row["title"] in titles)]


def replace_rights(session, record_id, rights):
    """Edit one published record so its rights are exactly `rights`; return it."""
    headers = {"Accept": RDM_JSON}
    draft_url = f"{ZENODO_API}/records/{record_id}/draft"
    opened = session.post(draft_url, headers=headers, timeout=(15, 60))
    opened.raise_for_status()
    try:
        draft = opened.json()
        draft["metadata"]["rights"] = rights
        updated = session.put(draft_url, json={key: draft[key] for key in EDITABLE if key in draft},
                              headers=headers, timeout=(15, 60))
        updated.raise_for_status()
        raise_draft_errors(updated.json())
        published = session.post(f"{draft_url}/actions/publish", headers=headers, timeout=(15, 120))
        published.raise_for_status()
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
    targets = legacy_targets(artifacts, user_records(session), set(args.title))
    total = len(targets)
    if args.limit:
        targets = targets[:args.limit]
    print(f"{ZENODO_API}: {total} legacy record(s), handling {len(targets)}"
          f"{'' if args.apply else ' (dry run, nothing is written)'}", flush=True)
    updated, failures = 0, 0
    for row in targets:
        line = f"{row['title']} {row['doi_url']}: {row['published_license']} -> {row['catalog_license']}"
        if not args.apply:
            print(f"would update {line}", flush=True)
            continue
        try:
            expected = rights_from_catalog(artifacts[row["title"]])
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
