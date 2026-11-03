"""Extract the original license declarations from a build recipe."""


def declared_licenses(recipe):
    entries = recipe.get("copyright") or []
    if not isinstance(entries, list):
        raise ValueError("Recipe copyright must be a list")
    licenses = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Recipe copyright entry must be an object")
        value = next((candidate.strip() for candidate in
                      (entry.get("license"), entry.get("name"))
                      if isinstance(candidate, str) and candidate.strip()), None)
        if value is None:
            raise ValueError("Recipe copyright entry needs a license or name")
        licenses.add(value)
    return sorted(licenses)


def license_details(recipe):
    details = []
    for entry in recipe.get("copyright") or []:
        identifier = entry.get("license") or ""
        if entry.get("name") or identifier.lower() in {"other-open", "other-closed"} or identifier.lower().startswith("licenseref-"):
            details.append({
                key: entry[key].strip()
                for key in ("license", "name", "url")
                if isinstance(entry.get(key), str) and entry[key].strip()
            })
    return details
