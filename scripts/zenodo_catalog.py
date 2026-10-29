#!/usr/bin/env python3
"""Publish missing container records and attach their DOIs to domain catalogs."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .doi_license import fetch_catalogue
from .doi_metadata import build_metadata, get_license


ZENODO_API = os.environ.get("ZENODO_API_ROOT", "https://zenodo.org/api").rstrip("/")
SIF_BASE = os.environ.get(
    "ZENODO_SIF_BASE_URL", "https://neurocontainers.s3.us-east-2.amazonaws.com"
).rstrip("/")
IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
BUILDDATE = re.compile(r"^[0-9]{8}$")
MAX_FILE_BYTES = 50_000_000_000


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


def artifact_name(app: dict[str, Any], name: str = "") -> str:
    image = re.sub(r"^[A-Za-z]+GUI-", "", name).replace(" ", "_")
    date = app.get("version")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError(f"Invalid catalog image: {image!r}")
    if not isinstance(date, str) or not BUILDDATE.fullmatch(date):
        raise ValueError(f"Invalid catalog build date: {date!r}")
    return f"{image}_{date}"


def catalog_artifacts(root: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for path in domain_paths(root):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict):
            raise ValueError(f"Invalid apps catalog: {path}")
        for tool, entry in catalog.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("apps"), dict):
                raise ValueError(f"Invalid app group in {path}: {tool}")
            for name, app in entry["apps"].items():
                if not isinstance(app, dict):
                    raise ValueError(f"Invalid app in {path}: {tool}/{name}")
                title = artifact_name(app, name)
                current = artifacts.setdefault(title, {"doi": "", "license": []})
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
    return artifacts


def has_production_doi(doi: Any) -> bool:
    return isinstance(doi, str) and bool(doi) and "10.5072/" not in doi


def published_depositions(session: requests.Session) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        response = session.get(
            f"{ZENODO_API}/deposit/depositions",
            params={"status": "published", "all_versions": 1, "page": page, "size": 100},
            timeout=(10, 60),
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Zenodo depositions response must be a list")
        for row in rows:
            title = row.get("title") or row.get("metadata", {}).get("title")
            if isinstance(title, str) and (
                title not in result or str(row.get("modified", "")) >
                str(result[title].get("modified", ""))
            ):
                result[title] = row
        if len(rows) < 100:
            return result
        page += 1


def missing_batches(
    artifacts: dict[str, dict[str, Any]],
    published: dict[str, dict[str, Any]],
    max_artifacts: int,
) -> list[list[str]]:
    if max_artifacts < 1:
        raise ValueError("max_artifacts must be positive")
    missing = [
        title for title, metadata in artifacts.items()
        if not has_production_doi(metadata["doi"]) and title not in published
    ]
    missing.sort(key=lambda title: (-int(title.rsplit("_", 1)[1]), title))
    selected = missing[:max_artifacts]
    if not selected:
        return [[]]
    batch_size = max(1, math.ceil(len(selected) / 4))
    return [selected[i:i + batch_size] for i in range(0, len(selected), batch_size)]


class RemoteStream:
    def __init__(self, response: requests.Response, length: int):
        self.response = response
        self.length = length

    def __len__(self) -> int:
        return self.length

    def read(self, size: int = -1) -> bytes:
        return self.response.raw.read(size)


def publish_deposition(
    title: str, license_info: dict[str, Any], session: requests.Session
) -> str:
    source_url = f"{SIF_BASE}/{quote(title, safe='')}.simg"
    # The public S3 request must never receive the Zenodo bearer token.
    download = requests.get(source_url, stream=True, timeout=(15, 300))
    try:
        download.raise_for_status()
        length = int(download.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_FILE_BYTES:
            raise ValueError(f"Unsupported SIF size for {title}: {length}")
        created = session.post(
            f"{ZENODO_API}/deposit/depositions", json={}, timeout=(15, 60)
        )
        created.raise_for_status()
        deposition = created.json()
        deposition_id = deposition["id"]
        try:
            uploaded = session.put(
                f"{deposition['links']['bucket']}/{quote(title, safe='')}.simg",
                data=RemoteStream(download, length),
                headers={"Content-Length": str(length)},
                timeout=(30, 300),
            )
            uploaded.raise_for_status()
            metadata = build_metadata(title, license_info)["metadata"]
            updated = session.put(
                f"{ZENODO_API}/deposit/depositions/{deposition_id}",
                json={"metadata": metadata}, timeout=(15, 60),
            )
            updated.raise_for_status()
            published = session.post(
                f"{ZENODO_API}/deposit/depositions/{deposition_id}/actions/publish",
                timeout=(15, 60),
            )
            published.raise_for_status()
            doi = published.json().get("doi")
            if not isinstance(doi, str) or not doi:
                raise ValueError(f"Published Zenodo deposition has no DOI: {title}")
            return doi
        except Exception:
            try:
                session.delete(
                    f"{ZENODO_API}/deposit/depositions/{deposition_id}",
                    timeout=(15, 60),
                )
            except requests.RequestException as error:
                print(f"Could not discard failed draft {deposition_id}: {error}", file=sys.stderr)
            raise
    finally:
        download.close()


def publish_batch(
    artifacts: dict[str, dict[str, Any]], selected: list[str], session: requests.Session,
    catalogue: dict[str, dict[str, Any]], github_token: str,
) -> int:
    existing = published_depositions(session)
    count = 0
    failures = []
    total = len(selected)
    for i, title in enumerate(selected, 1):
        prefix = f"[{i}/{total}] {title}"
        if title not in artifacts:
            raise ValueError(f"{prefix}: absent from domain catalogs")
        if has_production_doi(artifacts[title]["doi"]) or title in existing:
            print(f"{prefix}: already published, skipping", flush=True)
            continue
        try:
            print(f"{prefix}: resolving license ...", flush=True)
            license_info = get_license(title, github_token, catalogue)
            catalog_ids = {value.lower() for value in artifacts[title]["license"]}
            recipe_ids = {entry["id"] for entry in license_info["entries"]}
            if not catalog_ids or catalog_ids != recipe_ids:
                raise ValueError(
                    f"Catalog licenses {sorted(catalog_ids)!r} differ from "
                    f"build-time recipe {sorted(recipe_ids)!r}"
                )
            print(f"{prefix}: uploading to Zenodo ...", flush=True)
            doi = publish_deposition(title, license_info, session)
            existing[title] = {"doi": doi}
            count += 1
            print(f"{prefix}: published {doi}", flush=True)
        except Exception as exc:
            print(f"{prefix}: FAILED - {exc}", file=sys.stderr, flush=True)
            failures.append(title)
    if failures:
        raise RuntimeError(f"Failed to publish {len(failures)} artifact(s): {failures}")
    return count



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "publish"])
    parser.add_argument("--catalog-root", type=Path, default=Path("."))
    parser.add_argument("--max-artifacts", type=int, default=16)
    parser.add_argument("--artifacts-json")
    args = parser.parse_args()
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        parser.error("ZENODO_TOKEN is required")
    artifacts = catalog_artifacts(args.catalog_root)
    session = build_session(token)
    if args.command == "plan":
        print(json.dumps(missing_batches(
            artifacts, published_depositions(session), args.max_artifacts
        )))
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
