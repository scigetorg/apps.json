"""Read-only weekly audit: published Zenodo licenses against apps.json.

Build-time recipe licenses are validated where they enter the system:
consolidate writes them into apps.json, and publish refuses to mint a DOI when
apps.json and the build-time recipe disagree. This audit catches what can drift
afterwards, between each Zenodo record and the catalog entry for its title.

    Zenodo record (title, metadata.rights)          from /api/user/records
        │ title in apps.json?  no → skipped (retired image; DOI stays)
        ▼
    rights_from_catalog(apps.json) ─► same rights? ─► verified
        │                               └─ one license (old pipeline)    → verified_legacy
        │                               └─ several, differing            → mismatch
        └ no license ─► no_catalog_license
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .doi_license import license_equivalence_key, rights_from_catalog, rights_key
from .license_terms import OTHER_IDS
from .zenodo_catalog import build_session, catalog_artifacts, record_doi, user_records

# Zenodo's default license for software deposited without one. neurocommand's old
# publish-doi.py sent no license when a recipe had no `copyright`, so a lone
# cc-by-4.0 that apps.json does not list is that default, not a statement.
ZENODO_DEFAULT = ("id", license_equivalence_key("cc-by-4.0"))
PLACEHOLDERS = {("id", license_equivalence_key(other)) for other in OTHER_IDS}


def legacy_reason(published_key, expected_key):
    """Why a one-license record is a legacy-pipeline artefact, or None.

    neurocommand's publish-doi.py (deposit API, one license per record) sent:
    other-at when it failed to read the recipe, other-open for a name-only first
    entry, other-closed for matlab, nothing (-> Zenodo's cc-by-4.0) without
    copyright, and otherwise only the first license of the recipe on main at the
    time, not the published version's. So any one-license record is replaced by
    apps.json (recipe history); records with several licenses that differ stay a
    mismatch, since only the records API could have written them.
    """
    if len(published_key) != 1 or not expected_key:
        return None
    (single,) = published_key
    if single in PLACEHOLDERS:
        return "legacy placeholder category"
    if single == ZENODO_DEFAULT and single not in expected_key:
        return "Zenodo's default license (none was sent)"
    if published_key < expected_key:
        return "legacy one-license record, a subset of apps.json"
    return "legacy one-license record (the old pipeline read the first license of the recipe on main)"


def describe(rights):
    """Readable form of bare or Zenodo-expanded rights for the report."""
    labels = []
    for right in rights:
        if right.get("id"):
            labels.append(right["id"])
            continue
        title = right.get("title") or ""
        if isinstance(title, dict):
            title = title.get("en") or next(iter(title.values()), "")
        labels.append(f"{title} <{right.get('link') or 'no link'}>")
    return sorted(labels)


def audit_record(record, entry):
    metadata = record.get("metadata") or {}
    published = metadata.get("rights") or []
    doi = record_doi(record)
    row = {"id": record.get("id"), "title": metadata.get("title"),
           "doi_url": f"https://doi.org/{doi}" if doi else None,
           "published_license": describe(published)}
    if not entry.get("license"):
        row.update(status="no_catalog_license", problems=[],
                   reason="apps.json has no license for this image")
        return row
    try:
        expected = rights_from_catalog(entry)
    except ValueError as exc:
        row.update(status="mismatch", problems=[f"apps.json license is incomplete: {exc}"])
        return row
    row["catalog_license"] = describe(expected)
    published_key, expected_key = rights_key(published), rights_key(expected)
    if published_key == expected_key:
        row.update(status="verified", problems=[])
    elif reason := legacy_reason(published_key, expected_key):
        # The backfill replaces it with apps.json's licenses (recipe history).
        row.update(status="verified_legacy", problems=[], reason=reason)
    else:
        row.update(status="mismatch", problems=[
            f"Zenodo licenses {row['published_license']!r} differ from apps.json {row['catalog_license']!r}"
        ])
    return row


def audit_rows(artifacts, records):
    """Audit rows for published software records whose title is in apps.json."""
    return [audit_record(record, artifacts[title])
            for title, record in sorted(records.items())
            if title in artifacts and
            ((record.get("metadata") or {}).get("resource_type") or {}).get("id") == "software"]


def write_summary(path, report):
    lines = [
        "## Zenodo license audit",
        "",
        f"- Audited container records: {len(report['records'])}",
    ]
    for status, count in sorted(report["summary"].items()):
        lines.append(f"- {status}: {count}")
    lines.extend(["", "Detailed results are in the `zenodo-license-audit` artifact.", ""])
    findings = [row for row in report["records"] if row["status"] != "verified"]
    for row in findings[:100]:
        reason = "; ".join(row.get("problems") or []) or row.get("reason", "")
        lines.append(f"- **{row['status']}** `{row['title']}`: {reason}")
    if len(findings) > 100:
        lines.append(f"- … and {len(findings) - 100} more findings in the JSON artifact")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, default=Path("."))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    zenodo_token = os.environ.get("ZENODO_TOKEN")
    if not zenodo_token:
        parser.error("ZENODO_TOKEN is required")
    artifacts = catalog_artifacts(args.catalog_root)
    rows = audit_rows(artifacts, user_records(build_session(zenodo_token)))
    summary = {}
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "records": rows,
    }
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        write_summary(os.environ["GITHUB_STEP_SUMMARY"], report)
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary.get("mismatch") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        raise SystemExit(f"Audit failed: {exc}") from exc
