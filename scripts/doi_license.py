"""Validate recipe licenses against Zenodo's live license catalogue."""

import argparse
import difflib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from .license_terms import OTHER_IDS, is_custom_term


CATALOGUE_URL = "https://zenodo.org/api/licenses/"
UNKNOWN_NAMES = {"none", "unknown", "noassertion"}
# The "Other" categories offered when a recipe license needs review.
SUGGESTED_OTHER_IDS = frozenset({"other-open", "other-closed"})
# Deprecated SPDX IDs that Zenodo's legacy deposit API accepted, mapped to the
# IDs in its current catalogue so already-published recipes stay valid.
DEPRECATED_SPDX = {
    **{f"{family}-{version}": f"{family}-{version}-only" for family, version in (
        ("gpl", "1.0"), ("gpl", "2.0"), ("gpl", "3.0"), ("lgpl", "2.0"),
        ("lgpl", "2.1"), ("lgpl", "3.0"), ("agpl", "1.0"), ("agpl", "3.0"))},
    **{f"{family}-{version}+": f"{family}-{version}-or-later" for family, version in (
        ("gpl", "1.0"), ("gpl", "2.0"), ("gpl", "3.0"), ("lgpl", "2.0"),
        ("lgpl", "2.1"), ("lgpl", "3.0"))},
}


# Legacy spellings Zenodo has used for the same license. Comparison only: the
# ID chosen for publication still comes from the live catalogue.
LEGACY_IDS = {
    "mit-license": "mit",
    "apache2.0": "apache-2.0",
    "apgl-v3": "agpl-3.0",
    "bsd-2-clause-netbsd": "bsd-2-clause",
}


def license_equivalence_key(value):
    """Fold license IDs that name the same terms onto one comparison key."""
    key = value.strip().lower()
    key = LEGACY_IDS.get(key, key)
    key = DEPRECATED_SPDX.get(key, key)
    return key.removesuffix("-only")


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
        print(f"Fetched Zenodo license catalogue page {page} with {len(raw_hits)} entries")
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
    if not entries or not SUGGESTED_OTHER_IDS.issubset(entries):
        raise CatalogueError("Zenodo license catalogue is empty or lacks Other categories")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "entries": entries}


def load_catalogue(path):
    with open(path, encoding="utf-8") as stream:
        payload = json.load(stream)
    entries = payload.get("entries")
    if not isinstance(entries, dict) or not SUGGESTED_OTHER_IDS.issubset(entries):
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
        for category in sorted(SUGGESTED_OTHER_IDS):
            if category in catalogue and not any(row["id"] == category for row in candidates):
                candidates.append({"id": category, "title": catalogue[category]["title"], "reason": "custom terms; choose after review"})
                if len(candidates) == 3:
                    break
    return candidates


def is_custom_license(identifier):
    """Terms with no Zenodo catalogue ID: published by name and URL."""
    value = (identifier or "").strip().lower()
    return not value or value in OTHER_IDS or value.startswith("licenseref-")


def custom_right(name, url):
    return {"title": {"en": name}, "link": url}


def resolve_recipe_license(recipe, catalogue):
    """Return the recipe's licenses as Zenodo `rights` entries, one per copyright entry.

    A catalogue license becomes {"id": ...}; custom terms (a name, an other-*
    category or LicenseRef-) become {"title", "link"} and need a name and URL.
    Write these bare forms: Zenodo rejects its own expanded entries on update.
    """
    copyrights = recipe.get("copyright") if isinstance(recipe, dict) else None
    if not isinstance(copyrights, list) or not copyrights:
        raise LicenseValidationError("Recipe has no copyright entries; add license and URL")
    rights = []
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
        if is_custom_license(identifier):
            stated = name.strip() if isinstance(name, str) and name.strip() else None
            if stated is None and not identifier:
                raise LicenseValidationError(f"{context} needs a license ID or custom name")
            if stated is None:
                raise LicenseValidationError(
                    f"{context} custom license {identifier.strip()!r} needs a name and URL"
                )
            lowered = stated.lower()
            if lowered in UNKNOWN_NAMES or any(
                marker in lowered for marker in ("no licence published", "no license published", "does not declare a license")
            ):
                raise LicenseValidationError(
                    f"{context} does not identify usable license terms: {stated!r}",
                    suggest_licenses(stated, given_url, catalogue),
                )
            if not given_url:
                raise LicenseValidationError(f"{context} custom license {stated!r} needs a URL")
            right = custom_right(stated, given_url.strip())
        else:
            stated = identifier.strip()
            license_id = stated.lower()
            if license_id not in catalogue:
                license_id = DEPRECATED_SPDX.get(license_id, license_id)
            if license_id not in catalogue:
                raise LicenseValidationError(
                    f"{context} license {stated!r} is not a Zenodo catalogue ID; "
                    "choose an exact ID, or give custom terms a name and URL",
                    suggest_licenses(stated, given_url, catalogue),
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
            right = {"id": license_id}
        if right not in rights:
            rights.append(right)
    return {"rights": rights}


def rights_from_catalog(entry):
    """Rebuild bare `rights` from an apps.json entry's license and license_details.

    Catalogue IDs keep the recipe's spelling; compare with same_rights.
    """
    custom = [detail for detail in entry.get("license_details") or []
              if is_custom_term(detail.get("license"))]
    rights = []
    for detail in custom:
        if not detail.get("name") or not detail.get("url"):
            raise ValueError(f"Custom license {detail!r} needs a name and URL")
        rights.append(custom_right(detail["name"], detail["url"]))
    # apps.json lists custom terms by name (older entries by their other-*/LicenseRef- ID).
    custom_values = {value.strip().lower() for detail in custom
                     for value in (detail.get("license"), detail.get("name"))
                     if isinstance(value, str) and value.strip()}
    ids = sorted({value.strip().lower() for value in entry.get("license") or []} - custom_values)
    for value in ids:
        if is_custom_term(value):
            raise ValueError(f"apps.json has no license_details (name and URL) for {value!r}")
    return [{"id": value} for value in ids] + rights


def rights_key(rights):
    """Order-free comparison key for bare or Zenodo-expanded rights entries."""
    keys = set()
    for right in rights or []:
        if right.get("id"):
            keys.add(("id", license_equivalence_key(right["id"])))
            continue
        title = right.get("title") or ""
        if isinstance(title, dict):
            title = title.get("en") or next(iter(title.values()), "")
        link = right.get("link")
        keys.add(("custom", title.strip().lower(), canonical_url(link) if link else None))
    return frozenset(keys)


def same_rights(left, right):
    return rights_key(left) == rights_key(right)


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
