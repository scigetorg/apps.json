"""Resolve a container release's build-time recipe license and Zenodo metadata."""

import base64
from html import escape

import requests
import yaml

from .doi_license import LicenseValidationError, resolve_recipe_license
from .doi_provenance import ProvenanceServiceError, get_build_provenance

def get_license(container_name, gh_token, catalogue):
    """Resolve and validate the recipe at the image's build revision."""
    provenance = get_build_provenance(container_name, gh_token)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + gh_token,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = (
        "https://api.github.com/repos/Neurodesk/neurocontainers/contents/"
        f"recipes/{provenance['recipe']}/build.yaml"
    )
    try:
        response = requests.get(
            url, headers=headers, params={"ref": provenance["revision"]}, timeout=(15, 60)
        )
    except requests.RequestException as exc:
        raise ProvenanceServiceError(f"Could not fetch build-time recipe: {exc}") from exc
    if response.status_code == 404:
        raise LicenseValidationError(
            f"Recipe {provenance['recipe']} is absent at build revision {provenance['revision']}"
        )
    if not response.ok:
        raise ProvenanceServiceError(f"GitHub recipe API returned HTTP {response.status_code}")
    payload = response.json()
    if payload.get("encoding") == "base64" and payload.get("content"):
        recipe_content = base64.b64decode(payload["content"])
    else:
        raw_url = (
            "https://raw.githubusercontent.com/Neurodesk/neurocontainers/"
            f"{provenance['revision']}/recipes/{provenance['recipe']}/build.yaml"
        )
        try:
            raw_response = requests.get(raw_url, timeout=(15, 60))
        except requests.RequestException as exc:
            raise ProvenanceServiceError(f"Could not fetch large build-time recipe: {exc}") from exc
        if not raw_response.ok:
            raise ProvenanceServiceError(
                f"GitHub raw recipe endpoint returned HTTP {raw_response.status_code}"
            )
        recipe_content = raw_response.content
    try:
        recipe = yaml.safe_load(recipe_content)
    except (ValueError, yaml.YAMLError) as exc:
        raise LicenseValidationError(f"Build-time recipe YAML is invalid: {exc}") from exc
    if not isinstance(recipe, dict):
        raise LicenseValidationError("Build-time recipe YAML is not an object")
    license_info = resolve_recipe_license(recipe, catalogue)
    license_info.update(provenance)
    return license_info

def build_metadata(container_name, license_info):
    metadata = {
        "title": container_name,
        "upload_type": "software",
        "description": escape(container_name),
        "creators": [{"name": "Neurodesk", "affiliation": "University of Queensland"}],
        "license": license_info["zenodo_id"],
    }
    for entry in license_info["entries"]:
        name = escape(entry["name"])
        url = entry.get("url")
        if url:
            metadata["description"] += (
                f'<p>License: <a href="{escape(url, quote=True)}">{name}</a></p>'
            )
        else:
            metadata["description"] += f"<p>License: {name}</p>"
    revision = license_info["revision"]
    recipe = license_info["recipe"]
    recipe_url = f"https://github.com/neurodesk/neurocontainers/tree/{revision}/recipes/{recipe}"
    metadata["description"] += f'<p>Build recipe: <a href="{recipe_url}">{revision}</a></p>'
    return {"metadata": metadata}
