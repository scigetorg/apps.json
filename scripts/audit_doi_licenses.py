"""Read-only audit of published Zenodo software depositions."""

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from .zenodo_catalog import catalog_artifacts
from .doi_license import fetch_catalogue
from .doi_metadata import get_license


DEPOSITIONS_URL = "https://zenodo.org/api/deposit/depositions"
RELEASE_TITLE = re.compile(r".+_.+_[0-9]{8}\Z")

# Zenodo has used different IDs for the same license across catalogue updates.
LICENSE_ALIASES = {
    "mit-license": "mit",
    "apache2.0": "apache-2.0",
    "gpl-3.0-only": "gpl-3.0",
    "bsd-2-clause-netbsd": "bsd-2-clause",
    "gpl-2.0": "gpl-2.0-only",
    "gpl-2.0-only": "gpl-2.0",
    "lgpl-3.0": "lgpl-3.0-only",
    "apgl-v3": "agpl-3.0",
    "lgpl-2.1-or-later": "lgpl-2.1+",
    "lgpl-2.1": "lgpl-2.1-only",
}


def same_zenodo_license(actual, expected):
    return (actual == expected or LICENSE_ALIASES.get(actual) == expected
            or LICENSE_ALIASES.get(expected) == actual)


def is_release_title(title):
    return isinstance(title, str) and RELEASE_TITLE.fullmatch(title) is not None


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


def resolve_published_licenses(titles, github_token, catalogue, workers=8):
    """Read each published image's build-time recipe once for this audit."""
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get_license, title, github_token, catalogue): title
                   for title in titles}
        for future in as_completed(futures):
            title = futures[future]
            try:
                results[title] = future.result()
            except Exception as exc:
                results[title] = exc
    return results


def audit_deposition(deposition, artifacts, resolved):
    metadata = deposition.get("metadata") or {}
    title = deposition.get("title") or metadata.get("title") or ""
    actual_license = metadata.get("license")
    if isinstance(actual_license, dict):
        actual_license = actual_license.get("id")
    if isinstance(actual_license, str):
        actual_license = actual_license.lower()
    doi_url = deposition.get("doi_url")
    if not doi_url and deposition.get("doi"):
        doi_url = f"https://doi.org/{deposition['doi']}"
    row = {
        "id": deposition.get("id"),
        "title": title,
        "doi_url": doi_url,
        "published_license": actual_license,
    }
    if not is_release_title(title):
        row.update(status="unmatched", reason="Title does not match a container release name")
        return row
    catalog_entry = artifacts.get(title)
    row["catalog_status"] = "current" if catalog_entry is not None else "absent"
    info = resolved.get(title)
    if info is None or isinstance(info, Exception):
        reason = str(info) if info is not None else "No build-time recipe lookup result"
        row.update(status="unverified", reason=f"Could not verify build-time recipe: {reason}")
        return row
    problems = []
    expected_ids = sorted({value.lower() for value in info["declared"]})
    expected_details = info["license_details"]
    expected_zenodo = info["zenodo_id"]
    row["build_revision"] = info["revision"]
    row["expected_license"] = expected_ids
    row["expected_zenodo_license"] = expected_zenodo
    if info.get("image"):
        row["build_image"] = info["image"]
    if catalog_entry is not None:
        catalog_ids = sorted({v.lower() for v in catalog_entry["license"]})
        row["catalog_license"] = catalog_ids
        if catalog_ids != expected_ids:
            problems.append(f"apps.json licenses {catalog_ids!r} differ from build-time recipe {expected_ids!r}")
        if catalog_entry.get("license_details", []) != expected_details:
            problems.append("apps.json license details differ from build-time recipe")
    if not same_zenodo_license(actual_license, expected_zenodo):
        problems.append(f"Zenodo license {actual_license!r} differs from expected {expected_zenodo!r}")
    row.update(status="mismatch" if problems else "verified", problems=problems)
    return row


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
    depositions = []
    for deposition in fetch_depositions(zenodo_token):
        metadata = deposition.get("metadata") or {}
        resource = metadata.get("resource_type") or {}
        if not isinstance(resource, dict):
            resource = {}
        if metadata.get("upload_type") != "software" and resource.get("type") != "software":
            continue
        title = deposition.get("title") or metadata.get("title")
        if not is_release_title(title) or title not in artifacts:
            continue
        depositions.append(deposition)
    titles = {
        deposition.get("title") or (deposition.get("metadata") or {}).get("title")
        for deposition in depositions
    }
    resolved = (resolve_published_licenses(
        titles, os.environ.get("GITHUB_TOKEN"), fetch_catalogue()["entries"]
    ) if titles else {})
    rows = [audit_deposition(deposition, artifacts, resolved) for deposition in depositions]
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
    return 1 if summary.get("mismatch") or summary.get("unverified") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        raise SystemExit(f"Audit failed: {exc}") from exc
