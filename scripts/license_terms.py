"""Extract the original license declarations from a build recipe."""

import re

# Zenodo's catch-all "Other" licenses (Open, Not Open, Attribution, Non-Commercial,
# Public Domain). They name no actual terms, so a recipe using one must give the
# terms' name and URL; the legacy deposit API also used them when a record could
# carry only one license.
OTHER_IDS = frozenset({"other-open", "other-closed", "other-at", "other-nc", "other-pd"})

# Shape of a license catalogue ID such as "gpl-3.0-only".
CATALOGUE_ID = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")

# Values stating that no license is known: SPDX's NOASSERTION/NONE, "unknown",
# or a custom name saying none was published. The DOI is minted without
# `rights` for these entries (Zenodo's records API accepts a record without one).
NO_LICENSE_IDS = frozenset({"noassertion", "none", "unknown"})
NO_LICENSE_PHRASES = ("no licence published", "no license published",
                      "does not declare a license", "no license declared")


def is_no_license(value):
    """A license ID, name or apps.json label that states no license is known."""
    value = (value or "").strip().lower() if isinstance(value, str) else ""
    return value in NO_LICENSE_IDS or any(phrase in value for phrase in NO_LICENSE_PHRASES)


def declares_no_license(entry):
    """A recipe copyright entry (or apps.json license_details entry) with no known license.

    A real license ID wins over its name; otherwise the custom name decides.
    """
    identifier = entry.get("license")
    if is_no_license(identifier):
        return True
    if isinstance(identifier, str) and identifier.strip() and not is_custom_term(identifier):
        return False
    return is_no_license(entry.get("name"))


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
        # No-license entries keep their URL so readers can see where the source is.
        if entry.get("name") or is_custom_term(identifier) or declares_no_license(entry):
            details.append({
                key: entry[key].strip()
                for key in ("license", "name", "url")
                if isinstance(entry.get(key), str) and entry[key].strip()
            })
    return details
