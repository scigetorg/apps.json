"""Resolve a container release's build-time recipe license and Zenodo metadata."""

from datetime import datetime, timezone
from html import escape

import requests
import yaml

from .doi_license import LicenseValidationError, resolve_recipe_license
from .doi_provenance import ProvenanceServiceError, get_build_provenance
from .license_terms import declared_licenses, license_details


def fetch_recipe(recipe_name, source_repository, revision):
    """Read a recipe at an exact build revision from raw.githubusercontent.com.

    Raw file reads do not count against the GitHub REST API quota.
    """
    url = (f"https://raw.githubusercontent.com/{source_repository}/{revision}/"
           f"recipes/{recipe_name}/build.yaml")
    try:
        response = requests.get(url, timeout=(15, 60))
    except requests.RequestException as exc:
        raise ProvenanceServiceError(f"Could not fetch build-time recipe: {exc}") from exc
    if response.status_code == 404:
        raise LicenseValidationError(
            f"Recipe {recipe_name} is absent at build revision {revision}"
        )
    if not response.ok:
        raise ProvenanceServiceError(
            f"GitHub raw recipe endpoint returned HTTP {response.status_code}"
        )
    try:
        recipe = yaml.safe_load(response.content)
    except (ValueError, yaml.YAMLError) as exc:
        raise LicenseValidationError(f"Build-time recipe YAML is invalid: {exc}") from exc
    if not isinstance(recipe, dict):
        raise LicenseValidationError("Build-time recipe YAML is not an object")
    return recipe


def resolve_build_license(container_name, gh_token, source_repositories=None, catalogue=None):
    """Resolve the license declared by the recipe this image was built from.

    Strict: the OCI image must exist and carry its recipe revision labels.
    Consolidate passes no catalogue (it records declarations only and never
    depends on Zenodo); publish passes Zenodo's catalogue to get the deposit ID.
    """
    provenance = get_build_provenance(container_name, gh_token, source_repositories)
    recipe = fetch_recipe(provenance["recipe"], provenance["source_repository"],
                          provenance["revision"])
    info = resolve_recipe_license(recipe, catalogue) if catalogue is not None else {}
    info["declared"] = declared_licenses(recipe)
    info["license_details"] = license_details(recipe)
    info.update(provenance)
    return info


def build_metadata(container_name, license_info, today=None):
    """Draft body for Zenodo's records API (InvenioRDM)."""
    revision = license_info["revision"]
    recipe = license_info["recipe"]
    source_repository = license_info["source_repository"]
    recipe_url = f"https://github.com/{source_repository}/tree/{revision}/recipes/{recipe}"
    return {
        "metadata": {
            "resource_type": {"id": "software"},
            "title": container_name,
            "publication_date": (today or datetime.now(timezone.utc).date()).isoformat(),
            "creators": [{
                "person_or_org": {"type": "organizational", "name": "Neurodesk"},
                "affiliations": [{"name": "University of Queensland"}],
            }],
            # Zenodo requires a publisher to register the DOI.
            "publisher": "Zenodo",
            "description": (f"<p>{escape(container_name)}</p>"
                            f'<p>Build recipe: <a href="{recipe_url}">{revision}</a></p>'),
            "rights": license_info["rights"],
        },
        "files": {"enabled": True},
        "access": {"record": "public", "files": "public"},
    }
