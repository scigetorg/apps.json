#!/usr/bin/env python3
"""Publish missing container records and attach their DOIs to domain catalogs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .doi_license import fetch_catalogue, rights_from_catalog, same_rights
from .doi_metadata import build_metadata, resolve_build_license


ZENODO_API = os.environ.get("ZENODO_API_ROOT", "https://zenodo.org/api").rstrip("/")
SIF_BASE = os.environ.get(
    "ZENODO_SIF_BASE_URL", "https://neurocontainers.s3.us-east-2.amazonaws.com"
).rstrip("/")
IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
BUILDDATE = re.compile(r"^[0-9]{8}$")
MAX_FILE_BYTES = 50_000_000_000
# Without this Accept header Zenodo answers /api/user/records in its legacy
# format, which shows only the first license and no `pids`.
RDM_JSON = "application/vnd.inveniordm.v1+json"
TITLES_PER_QUERY = 50


def build_session(token: str) -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def domain_paths(root: Path) -> list[Path]:
    registry = json.loads((root / "domains.json").read_text(encoding="utf-8"))
    domains = registry.get("domains")
    if not isinstance(domains, dict) or not domains:
        raise ValueError("domains.json must list configured domains")
    return [root / domain / "apps.json" for domain in sorted(domains)]


def artifact_name(app: dict[str, Any]) -> str:
    # consolidate writes the exact SIF image on every launcher; it cannot be
    # derived from launcher names (GUI and arm64 releases differ).
    image = app.get("image")
    date = app.get("version")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError(f"Invalid catalog image: {image!r}")
    if not isinstance(date, str) or not BUILDDATE.fullmatch(date):
        raise ValueError(f"Invalid catalog build date: {date!r}")
    return f"{image}_{date}"


def catalog_artifacts(root: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    domains = json.loads((root / "domains.json").read_text(encoding="utf-8"))["domains"]
    for path in domain_paths(root):
        sources = domains[path.parent.name].get("source_repositories")
        if not isinstance(sources, list) or not sources or any(
            not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source)
            for source in sources
        ) or len(set(sources)) != len(sources):
            raise ValueError(f"Invalid source_repositories for {path.parent.name}")
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict):
            raise ValueError(f"Invalid apps catalog: {path}")
        for tool, entry in catalog.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("apps"), dict):
                raise ValueError(f"Invalid app group in {path}: {tool}")
            for name, app in entry["apps"].items():
                if not isinstance(app, dict):
                    raise ValueError(f"Invalid app in {path}: {tool}/{name}")
                title = artifact_name(app)
                current = artifacts.setdefault(title, {"doi": "", "license": [],
                                                       "license_details": [], "source_repositories": []})
                current["source_repositories"] = sorted(set(
                    current["source_repositories"] + sources
                ))
                doi = app.get("doi") or ""
                if doi and current["doi"] and doi != current["doi"]:
                    raise ValueError(f"Conflicting DOI for {title}")
                if doi:
                    current["doi"] = doi
                licenses = app.get("license") or []
                if isinstance(licenses, str):
                    licenses = [licenses]
                if not isinstance(licenses, list):
                    raise ValueError(f"Invalid license for {tool}/{name}")
                if licenses:
                    normalized = sorted(set(licenses))
                    if current["license"] and current["license"] != normalized:
                        raise ValueError(f"Conflicting licenses for {title}")
                    current["license"] = normalized
                details = app.get("license_details") or []
                if not isinstance(details, list) or any(not isinstance(item, dict) for item in details):
                    raise ValueError(f"Invalid license details for {tool}/{name}")
                if details:
                    if current["license_details"] and current["license_details"] != details:
                        raise ValueError(f"Conflicting license details for {title}")
                    current["license_details"] = details
    return artifacts


def has_production_doi(doi: Any) -> bool:
    return isinstance(doi, str) and bool(doi) and "10.5072/" not in doi


def user_records(
    session: requests.Session, titles: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Published records of this account by exact title, newest wins.

    Zenodo's search stops at about 10,000 results, far above this catalog.
    """
    if titles is not None and not titles:
        return {}
    queries = ["is_published:true"] if titles is None else [
        "is_published:true AND (" + " OR ".join(
            f'metadata.title:"{title}"' for title in chunk) + ")"
        for chunk in (sorted(titles)[i:i + TITLES_PER_QUERY]
                      for i in range(0, len(titles), TITLES_PER_QUERY))
    ]
    result: dict[str, dict[str, Any]] = {}
    for query in queries:
        page = 1
        while True:
            response = session.get(
                f"{ZENODO_API}/user/records",
                params={"q": query, "allversions": "true", "sort": "newest",
                        "page": page, "size": 100},
                headers={"Accept": RDM_JSON},
                timeout=(10, 60),
            )
            response.raise_for_status()
            hits = (response.json().get("hits") or {}).get("hits")
            if not isinstance(hits, list):
                raise ValueError("Zenodo records response has no hits list")
            for row in hits:
                title = (row.get("metadata") or {}).get("title")
                if (row.get("is_published") is not True or not isinstance(title, str)
                        or (titles is not None and title not in titles)):
                    continue
                if title not in result or str(row.get("updated", "")) > str(result[title].get("updated", "")):
                    result[title] = row
            if len(hits) < 100:
                break
            page += 1
    return result


def record_doi(row: dict[str, Any]) -> str | None:
    return ((row.get("pids") or {}).get("doi") or {}).get("identifier")


# Keep in step with `max-parallel` in .github/workflows/publish-dois.yml.
PUBLISH_JOBS = 6


def missing_batches(
    artifacts: dict[str, dict[str, Any]],
    published: dict[str, dict[str, Any]],
    jobs: int = PUBLISH_JOBS,
) -> list[list[str]]:
    """Split every unpublished image across at most `jobs` parallel batches."""
    if jobs < 1:
        raise ValueError("jobs must be positive")
    missing = [
        title for title, metadata in artifacts.items()
        if not has_production_doi(metadata["doi"]) and title not in published
    ]
    if not missing:
        return [[]]
    missing.sort(key=lambda title: (-int(title.rsplit("_", 1)[1]), title))
    # Round-robin keeps batches within one image of each other and starts the
    # newest builds first in every job.
    return [missing[i::jobs] for i in range(min(jobs, len(missing)))]


class RemoteStream:
    def __init__(self, response: requests.Response, length: int):
        self.response = response
        self.length = length

    def __len__(self) -> int:
        return self.length

    def read(self, size: int = -1) -> bytes:
        return self.response.raw.read(size)


def raise_draft_errors(body: dict[str, Any], ignore: tuple[str, ...] = ()) -> None:
    # Draft writes answer HTTP 200/201 even when fields fail validation.
    errors = [error for error in body.get("errors") or [] if error.get("field") not in ignore]
    if errors:
        raise ValueError(f"Zenodo rejected draft fields: {errors}")


def publish_deposition(
    title: str, license_info: dict[str, Any], session: requests.Session
) -> str:
    """Create a draft, stream the SIF from S3 into it, and publish; return the DOI."""
    source_url = f"{SIF_BASE}/{quote(title, safe='')}.simg"
    # The public S3 request must never receive the Zenodo bearer token.
    download = requests.get(source_url, stream=True, timeout=(15, 300))
    try:
        download.raise_for_status()
        length = int(download.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_FILE_BYTES:
            raise ValueError(f"Unsupported SIF size for {title}: {length}")
        headers = {"Accept": RDM_JSON}
        created = session.post(f"{ZENODO_API}/records", json=build_metadata(title, license_info),
                               headers=headers, timeout=(15, 60))
        created.raise_for_status()
        draft = created.json()
        draft_url = f"{ZENODO_API}/records/{draft['id']}/draft"
        try:
            # The file is attached next, so "missing uploaded files" is expected here.
            raise_draft_errors(draft, ignore=("files.enabled",))
            key = f"{title}.simg"
            file_url = f"{draft_url}/files/{quote(key, safe='')}"
            session.post(f"{draft_url}/files", json=[{"key": key}], headers=headers,
                         timeout=(15, 60)).raise_for_status()
            session.put(f"{file_url}/content", data=RemoteStream(download, length),
                        headers={**headers, "Content-Type": "application/octet-stream",
                                 "Content-Length": str(length)},
                        timeout=(30, 300)).raise_for_status()
            committed = session.post(f"{file_url}/commit", headers=headers, timeout=(30, 600))
            committed.raise_for_status()
            if committed.json().get("size") != length:
                raise ValueError(f"Zenodo stored {committed.json().get('size')} bytes, expected {length}")
            published = session.post(f"{draft_url}/actions/publish", headers=headers,
                                     timeout=(15, 120))
            published.raise_for_status()
            record = published.json()
            raise_draft_errors(record)
            doi = record_doi(record)
            if not isinstance(doi, str) or not doi:
                raise ValueError(f"Published Zenodo record has no DOI: {title}")
            return doi
        except Exception:
            try:
                session.delete(draft_url, headers=headers, timeout=(15, 60))
            except requests.RequestException as error:
                print(f"Could not discard failed draft {draft['id']}: {error}", file=sys.stderr)
            raise
    finally:
        download.close()


def publish_batch(
    artifacts: dict[str, dict[str, Any]], selected: list[str], session: requests.Session,
    catalogue: dict[str, dict[str, Any]], github_token: str,
) -> int:
    existing = user_records(session, set(selected))
    count = 0
    failures = {}
    total = len(selected)
    for i, title in enumerate(selected, 1):
        prefix = f"[{i}/{total}] {title}"
        try:
            if title not in artifacts:
                raise ValueError("absent from domain catalogs")
            if has_production_doi(artifacts[title]["doi"]) or title in existing:
                print(f"{prefix}: already published, skipping", flush=True)
                continue
            print(f"{prefix}: resolving license ...", flush=True)
            license_info = resolve_build_license(
                title, github_token, artifacts[title].get("source_repositories"), catalogue)
            expected = rights_from_catalog(artifacts[title])
            if not expected or not same_rights(license_info["rights"], expected):
                raise ValueError(
                    f"Catalog licenses {expected!r} differ from "
                    f"build-time recipe {license_info['rights']!r}"
                )
            print(f"{prefix}: uploading to Zenodo ...", flush=True)
            doi = publish_deposition(title, license_info, session)
            existing[title] = {"pids": {"doi": {"identifier": doi}}}
            count += 1
            print(f"{prefix}: published {doi}", flush=True)
        except Exception as exc:
            print(f"{prefix}: FAILED - {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            failures[title] = f"{type(exc).__name__}: {exc}"
    if failures:
        summary = "\n".join(f"  {title}: {error}" for title, error in failures.items())
        raise RuntimeError(
            f"Published {count}; failed to publish {len(failures)} artifact(s):\n{summary}"
        )
    return count



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "publish", "lookup"])
    parser.add_argument("--catalog-root", type=Path, default=Path("."))
    parser.add_argument("--artifacts-json")
    args = parser.parse_args()
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        parser.error("ZENODO_TOKEN is required")
    session = build_session(token)
    if args.command == "lookup":
        print(json.dumps({title: record_doi(row) for title, row in user_records(session).items()
                          if record_doi(row)}))
        return 0
    artifacts = catalog_artifacts(args.catalog_root)
    if args.command == "plan":
        print(json.dumps(missing_batches(artifacts, user_records(session))))
    elif args.command == "publish":
        if args.artifacts_json is None:
            parser.error("--artifacts-json is required for publish")
        selected = json.loads(args.artifacts_json)
        if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected):
            parser.error("--artifacts-json must be a JSON list of names")
        github_token = os.environ.get("GITHUB_TOKEN", "")
        catalogue = fetch_catalogue()["entries"]
        print(f"Published {publish_batch(artifacts, selected, session, catalogue, github_token)} Zenodo records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
