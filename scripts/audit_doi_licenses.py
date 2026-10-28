"""Read-only audit of published Zenodo software depositions."""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .zenodo_catalog import catalog_artifacts


DEPOSITIONS_URL = "https://zenodo.org/api/deposit/depositions"


def fetch_depositions(token):
    depositions = []
    page = 1
    while True:
        for attempt in range(3):
            try:
                response = requests.get(
                    DEPOSITIONS_URL,
                    params={"status": "published", "all_versions": 1, "page": page, "size": 100},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=(15, 60),
                )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise RuntimeError(f"Could not list Zenodo depositions: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise RuntimeError(
                        f"Zenodo deposition list returned HTTP {response.status_code}"
                    )
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                raise RuntimeError(
                    f"Zenodo deposition list returned HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            break
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError("Zenodo deposition list response was not an array")
        depositions.extend(batch)
        if len(batch) < 100:
            return depositions
        page += 1


def audit_deposition(deposition, artifacts):
    metadata = deposition.get("metadata") or {}
    title = deposition.get("title") or metadata.get("title") or ""
    actual_license = metadata.get("license")
    if isinstance(actual_license, dict):
        actual_license = actual_license.get("id")
    doi_url = deposition.get("doi_url")
    if not doi_url and deposition.get("doi"):
        doi_url = f"https://doi.org/{deposition['doi']}"
    row = {
        "id": deposition.get("id"),
        "title": title,
        "doi_url": doi_url,
        "published_license": actual_license,
    }
    if not re.fullmatch(r".+_.+_[0-9]{8}", title):
        row.update(status="unmatched", reason="Title does not match a container release name")
        return row
    if title not in artifacts:
        row.update(status="unmatched", reason="Title not found in apps.json catalog")
        return row
    catalog_ids = sorted({v.lower() for v in artifacts[title]["license"]})
    row["catalog_license"] = catalog_ids
    problems = []
    if not catalog_ids:
        problems.append("apps.json has no license for this container")
    elif len(catalog_ids) == 1:
        if actual_license != catalog_ids[0]:
            problems.append(f"Zenodo license {actual_license!r} differs from apps.json {catalog_ids[0]!r}")
    elif actual_license not in {"other-open", "other-closed"}:
        problems.append(
            f"Zenodo license {actual_license!r} is not other-open/other-closed for multi-license container"
        )
    row.update(status="mismatch" if problems else "verified", problems=problems)
    return row


def write_summary(path, report):
    lines = [
        "## Zenodo license audit",
        "",
        f"- Published software records: {len(report['records'])}",
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


def load_previous(path):
    """Return verified rows from a previous audit keyed by deposition ID."""
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        row["id"]: row
        for row in prev.get("records", [])
        if row.get("status") == "verified" and row.get("id") is not None
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, default=Path("."))
    parser.add_argument("--output", required=True)
    parser.add_argument("--previous", metavar="PATH",
                        help="Previous audit JSON; verified records are reused unless catalog licenses changed")
    args = parser.parse_args()
    zenodo_token = os.environ.get("ZENODO_TOKEN")
    if not zenodo_token:
        parser.error("ZENODO_TOKEN is required")
    artifacts = catalog_artifacts(args.catalog_root)
    cache = load_previous(args.previous) if args.previous else {}
    rows = []
    for deposition in fetch_depositions(zenodo_token):
        metadata = deposition.get("metadata") or {}
        resource = metadata.get("resource_type") or {}
        if not isinstance(resource, dict):
            resource = {}
        if metadata.get("upload_type") != "software" and resource.get("type") != "software":
            continue
        dep_id = deposition.get("id")
        cached = cache.get(dep_id)
        if cached is not None:
            title = cached.get("title", "")
            if title in artifacts:
                current = sorted({v.lower() for v in artifacts[title]["license"]})
                if cached.get("catalog_license") != current:
                    cached = None
        rows.append(cached if cached is not None else audit_deposition(deposition, artifacts))
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


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"Audit failed: {exc}") from exc
