"""Zenodo records-API metadata for a container image."""

from datetime import datetime, timezone
from html import escape


def build_metadata(container_name, license_info, today=None):
    """Draft body for Zenodo's records API (InvenioRDM).

    `license_info` comes from recipe_history: the recipe commit is the newest one
    declaring the image's version, which can be later than the build itself.
    """
    revision = license_info["revision"]
    recipe = license_info["recipe"]
    version = license_info["version"]
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
                            f"<p>Recipe for version {escape(version)}: "
                            f'<a href="{recipe_url}">{revision}</a></p>'),
            "rights": license_info["rights"],
        },
        "files": {"enabled": True},
        "access": {"record": "public", "files": "public"},
    }
