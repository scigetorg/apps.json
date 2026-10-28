"""Validate recipe licenses against Zenodo's live license catalogue."""

import argparse
import difflib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests


CATALOGUE_URL = "https://zenodo.org/api/licenses/"
OTHER_IDS = {"other-open", "other-closed"}
UNKNOWN_NAMES = {"none", "unknown", "noassertion"}


class LicenseValidationError(ValueError):
    def __init__(self, message, suggestions=None):
        super().__init__(message)
        self.suggestions = suggestions or []

    def as_dict(self):
        return {"kind": "validation", "message": str(self), "suggestions": self.suggestions}


class CatalogueError(RuntimeError):
    pass


def _get_with_retry(url, **kwargs):
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=(15, 60), **kwargs)
        except requests.RequestException as exc:
            if attempt == 2:
                raise CatalogueError(f"Zenodo license catalogue unavailable: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                raise CatalogueError(
                    f"Zenodo license catalogue returned HTTP {response.status_code}"
                )
            delay = response.headers.get("Retry-After", "")
            time.sleep(min(int(delay), 60) if delay.isdigit() else 2 ** attempt)
            continue
        if not response.ok:
            raise CatalogueError(
                f"Zenodo license catalogue returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response
    raise CatalogueError("Zenodo license catalogue unavailable")


def _catalogue_entry(raw):
    if not isinstance(raw, dict):
        raise CatalogueError("Zenodo licence catalogue contains a non-object entry")
    metadata = raw.get("metadata") or {}
    props = raw.get("props") or {}
    license_id = raw.get("id") or metadata.get("id")
    if not isinstance(license_id, str) or not license_id.strip():
        raise CatalogueError("Zenodo licence catalogue entry has no ID")
    title = raw.get("title") or metadata.get("title") or license_id
    if isinstance(title, dict):
        title = title.get("en") or next(iter(title.values()), license_id)
    url = props.get("url") or metadata.get("url") or raw.get("url")
    approved = props.get("osi_approved", metadata.get("osi_approved", False))
    return {
        "id": license_id.strip().lower(),
        "title": str(title),
        "url": url if isinstance(url, str) else None,
        "osi_approved": approved is True or str(approved).lower() in {"y", "yes", "true", "1"},
    }


def fetch_catalogue():
    entries = {}
    page = 1
    while True:
        response = _get_with_retry(CATALOGUE_URL, params={"page": page, "size": 100})
        payload = response.json()
        print(f"Fetched Zenodo license catalogue page {page} with {len(payload.get('hits', []))} entries: {response.url}")
        if isinstance(payload, dict):
            raw_hits = payload.get("hits")
            total = payload.get("total")
            if isinstance(raw_hits, dict):
                total = raw_hits.get("total")
                raw_hits = raw_hits.get("hits")
            if isinstance(total, dict):
                total = total.get("value")
        else:
            raw_hits, total = payload, None
        if not isinstance(raw_hits, list):
            raise CatalogueError("Unexpected Zenodo license catalogue response")
        for raw in raw_hits:
            entry = _catalogue_entry(raw)
            entries[entry["id"]] = entry
        if not raw_hits or (isinstance(total, int) and len(entries) >= total):
            break
        if len(raw_hits) < 100 and total is None:
            break
        page += 1
        if page > 100:
            raise CatalogueError("Zenodo license catalogue pagination did not finish")
    if not entries or not OTHER_IDS.issubset(entries):
        raise CatalogueError("Zenodo license catalogue is empty or lacks Other categories")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "entries": entries}


def load_catalogue(path):
    with open(path, encoding="utf-8") as stream:
        payload = json.load(stream)
    entries = payload.get("entries")
    if not isinstance(entries, dict) or not OTHER_IDS.issubset(entries):
        raise CatalogueError("Downloaded Zenodo license catalogue is incomplete")
    return entries


def canonical_url(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LicenseValidationError(f"Invalid license URL: {value!r}")
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise LicenseValidationError(f"Invalid license URL: {value!r}")
    if parts.username or parts.password:
        raise LicenseValidationError("License URL must not contain credentials")
    return (parts.netloc.lower(), parts.path.rstrip("/"), parts.query)


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def suggest_licenses(value, url, catalogue):
    candidates = []
    target_url = canonical_url(url) if url else None
    if target_url:
        for entry in catalogue.values():
            try:
                matches = entry.get("url") and canonical_url(entry["url"]) == target_url
            except LicenseValidationError:
                matches = False
            if matches:
                candidates.append({"id": entry["id"], "title": entry["title"], "reason": "exact URL match"})
    if len(candidates) >= 3:
        return candidates[:3]

    target = _slug(value)
    ranked = []
    for entry in catalogue.values():
        if any(item["id"] == entry["id"] for item in candidates):
            continue
        id_slug = _slug(entry["id"])
        title_slug = _slug(entry["title"])
        # A shorter name must not be suggested by discarding a legal modifier.
        if target.startswith(id_slug + "-") or target.startswith(title_slug + "-"):
            continue
        score = max(
            difflib.SequenceMatcher(None, target, id_slug).ratio(),
            difflib.SequenceMatcher(None, target, title_slug).ratio(),
        )
        if score >= 0.58:
            ranked.append((score, entry))
    for _, entry in sorted(ranked, key=lambda row: (-row[0], row[1]["id"])):
        candidates.append({"id": entry["id"], "title": entry["title"], "reason": "similar name; verify terms"})
        if len(candidates) == 3:
            break
    if len(candidates) < 3 and (
        not candidates or value.lower().startswith("licenseref-") or value.lower() in {"custom", "custome"}
    ):
        for category in sorted(OTHER_IDS):
            if category in catalogue and not any(row["id"] == category for row in candidates):
                candidates.append({"id": category, "title": catalogue[category]["title"], "reason": "custom terms; choose after review"})
                if len(candidates) == 3:
                    break
    return candidates


def _category(value, catalogue, context):
    if not isinstance(value, str) or value.strip().lower() not in OTHER_IDS:
        raise LicenseValidationError(f"{context} must be other-open or other-closed")
    category = value.strip().lower()
    if category not in catalogue:
        raise CatalogueError(f"Zenodo catalogue does not contain {category}")
    return category


def resolve_recipe_license(recipe, catalogue):
    """Return an exact Zenodo ID and every declared license for the description."""
    copyrights = recipe.get("copyright") if isinstance(recipe, dict) else None
    if not isinstance(copyrights, list) or not copyrights:
        raise LicenseValidationError("Recipe has no copyright entries; add license and URL")
    resolved = []
    for number, item in enumerate(copyrights, 1):
        context = f"copyright entry {number}"
        if not isinstance(item, dict):
            raise LicenseValidationError(f"{context} must be an object")
        identifier = item.get("license")
        name = item.get("name")
        given_url = item.get("url")
        if given_url is not None:
            canonical_url(given_url)
        if "license" in item and (not isinstance(identifier, str) or not identifier.strip()):
            raise LicenseValidationError(f"{context} license must be a non-empty string")
        if isinstance(identifier, str) and identifier.strip():
            stated = identifier.strip()
            license_id = stated.lower()
            if license_id not in catalogue:
                raise LicenseValidationError(
                    f"{context} license {stated!r} is not a Zenodo catalogue ID; "
                    "choose an exact ID or other-open/other-closed with a URL",
                    suggest_licenses(stated, given_url, catalogue),
                )
            if item.get("zenodo_license") is not None:
                raise LicenseValidationError(
                    f"{context} already has a catalogue ID; remove its zenodo_license override"
                )
            if license_id in OTHER_IDS and not given_url:
                raise LicenseValidationError(f"{context} {license_id} needs a license URL")
            catalogue_url = catalogue[license_id].get("url")
            if not given_url and not catalogue_url:
                raise LicenseValidationError(
                    f"{context} {license_id} has no URL in the recipe or Zenodo catalogue"
                )
            if given_url:
                matches = []
                for entry in catalogue.values():
                    try:
                        same_url = entry.get("url") and canonical_url(entry["url"]) == canonical_url(given_url)
                    except LicenseValidationError:
                        same_url = False
                    if same_url:
                        matches.append(entry)
                if matches and not any(entry["id"] == license_id for entry in matches):
                    raise LicenseValidationError(
                        f"{context} URL matches a different Zenodo license, not {stated!r}",
                        [{"id": entry["id"], "title": entry["title"], "reason": "exact URL match"} for entry in matches[:3]],
                    )
            resolved.append({
                "id": license_id,
                "name": (
                    name.strip()
                    if license_id in OTHER_IDS and isinstance(name, str) and name.strip()
                    else stated
                ),
                "url": given_url or catalogue_url,
                "osi_approved": catalogue[license_id]["osi_approved"],
                "custom": license_id in OTHER_IDS,
            })
        elif isinstance(name, str) and name.strip():
            stated = name.strip()
            lowered = stated.lower()
            if lowered in UNKNOWN_NAMES or any(
                marker in lowered for marker in ("no licence published", "no license published", "does not declare a license")
            ):
                raise LicenseValidationError(
                    f"{context} does not identify usable license terms: {stated!r}",
                    suggest_licenses(stated, given_url, catalogue),
                )
            if not given_url:
                raise LicenseValidationError(f"{context} name-only custom license needs a URL")
            category = (
                _category(item["zenodo_license"], catalogue, f"{context} zenodo_license")
                if item.get("zenodo_license") is not None else "other-open"
            )
            resolved.append({"id": category, "name": stated, "url": given_url, "osi_approved": False, "custom": True})
        else:
            raise LicenseValidationError(f"{context} needs a license ID or custom name")

    ids = {item["id"] for item in resolved}
    override = recipe.get("zenodo_license")
    if override is not None:
        if len(resolved) < 2:
            raise LicenseValidationError("Top-level zenodo_license is only for multiple copyright entries")
        zenodo_id = _category(override, catalogue, "Top-level zenodo_license")
        if zenodo_id == "other-open" and any(item["id"] == "other-closed" for item in resolved):
            raise LicenseValidationError(
                "Top-level other-open conflicts with an other-closed copyright entry"
            )
    elif len(ids) == 1:
        zenodo_id = next(iter(ids))
    elif all(item["osi_approved"] and not item["custom"] for item in resolved):
        zenodo_id = "other-open"
    else:
        raise LicenseValidationError(
            "Multiple licenses cannot be classified safely; add top-level "
            "zenodo_license: other-open or other-closed"
        )
    return {"zenodo_id": zenodo_id, "entries": resolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", metavar="PATH", help="Save the current Zenodo catalogue as JSON")
    args = parser.parse_args()
    if not args.fetch:
        parser.error("--fetch is required")
    catalogue = fetch_catalogue()
    with open(args.fetch, "w", encoding="utf-8") as stream:
        json.dump(catalogue, stream, indent=2)
    print(f"Fetched {len(catalogue['entries'])} Zenodo licenses")


if __name__ == "__main__":
    main()
