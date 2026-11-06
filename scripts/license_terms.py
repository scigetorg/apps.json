"""Extract the original license declarations from a build recipe."""

import re

# Zenodo's catch-all "Other" licenses (Open, Not Open, Attribution, Non-Commercial,
# Public Domain). They name no actual terms, so a recipe using one must give the
# terms' name and URL; the legacy deposit API also used them when a record could
# carry only one license.
OTHER_IDS = frozenset({"other-open", "other-closed", "other-at", "other-nc", "other-pd"})

# Shape of a license catalogue ID such as "gpl-3.0-only".
CATALOGUE_ID = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")


def is_custom_term(value):
    """A declared license with no license ID: other-*, LicenseRef-…, or free text.

    Such terms need their name and URL kept in license_details. Mirrored by
    isCustomLicense in scripts/catalog.mjs.
    """
    value = (value or "").strip().lower()
    return (value in OTHER_IDS or value.startswith("licenseref-")
            or not CATALOGUE_ID.fullmatch(value))


def license_label(entry):
    """How a copyright entry is listed in apps.json `license`.

    Custom terms (other-*, LicenseRef-…, free text) are listed by their `name`
    when they have one, so readers see e.g. "FSL license" rather than
    "other-closed"; catalogue IDs such as "MIT" are listed as the ID.
    """
    license_id, name = (value.strip() if isinstance(value, str) else ""
                        for value in (entry.get("license"), entry.get("name")))
    return name if name and is_custom_term(license_id) else (license_id or name)


def declared_licenses(recipe):
    entries = recipe.get("copyright") or []
    if not isinstance(entries, list):
        raise ValueError("Recipe copyright must be a list")
    licenses = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Recipe copyright entry must be an object")
        value = license_label(entry)
        if not value:
            raise ValueError("Recipe copyright entry needs a license or name")
        licenses.add(value)
    return sorted(licenses)


def license_details(recipe):
    details = []
    for entry in recipe.get("copyright") or []:
        identifier = entry.get("license") or ""
        if entry.get("name") or is_custom_term(identifier):
            details.append({
                key: entry[key].strip()
                for key in ("license", "name", "url")
                if isinstance(entry.get(key), str) and entry[key].strip()
            })
    return details
