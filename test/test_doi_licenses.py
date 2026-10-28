"""Build-time license validation and published DOI audit contracts."""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import doi_license, doi_metadata, doi_provenance
from scripts.audit_doi_licenses import audit_deposition
from scripts.zenodo_catalog import publish_batch


def catalogue():
    return {
        "mit": {"id": "mit", "title": "MIT License", "url": "https://spdx.org/licenses/MIT.html", "osi_approved": True},
        "apache-2.0": {"id": "apache-2.0", "title": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0", "osi_approved": True},
        "other-open": {"id": "other-open", "title": "Other Open", "url": None, "osi_approved": False},
        "other-closed": {"id": "other-closed", "title": "Other Closed", "url": None, "osi_approved": False},
    }


class DoiLicenseTests(unittest.TestCase):
    def test_exact_zenodo_id_and_custom_terms(self):
        info = doi_license.resolve_recipe_license(
            {"copyright": [{"license": "MIT"}]}, catalogue()
        )
        self.assertEqual(info["zenodo_id"], "mit")
        with self.assertRaises(doi_license.LicenseValidationError):
            doi_license.resolve_recipe_license(
                {"copyright": [{"license": "MIT-Modified", "url": "https://example.org/terms"}]},
                catalogue(),
            )
        info = doi_license.resolve_recipe_license(
            {"copyright": [{"name": "Research terms", "url": "https://example.org/terms",
                            "zenodo_license": "other-closed"}]}, catalogue()
        )
        self.assertEqual(info["zenodo_id"], "other-closed")

    def test_recipe_is_fetched_at_image_build_revision(self):
        revision = "a" * 40
        recipe = {"copyright": [{"license": "MIT"}]}
        response = SimpleNamespace(
            ok=True, status_code=200,
            json=lambda: {"encoding": "base64", "content": base64.b64encode(json.dumps(recipe).encode()).decode()},
        )
        with (patch.object(doi_metadata, "get_build_provenance", return_value={
            "revision": revision, "recipe": "demo", "image": "ghcr.io/neurodesk/demo_1.0:20260101",
        }), patch.object(doi_metadata.requests, "get", return_value=response) as get):
            info = doi_metadata.get_license("demo_1.0_20260101", "token", catalogue())
        self.assertEqual(get.call_args.kwargs["params"], {"ref": revision})
        self.assertEqual(info["zenodo_id"], "mit")

    def test_image_labels_must_match_container(self):
        revision = "a" * 40
        labels = {
            "org.opencontainers.image.revision": revision,
            "org.opencontainers.image.title": "demo",
            "org.opencontainers.image.version": "1.0_20260101",
            "org.opencontainers.image.source": "https://github.com/neurodesk/neurocontainers",
            "org.opencontainers.image.documentation": f"https://github.com/neurodesk/neurocontainers/tree/{revision}/recipes/demo",
        }
        with patch.object(doi_provenance.RegistryReader, "image_labels", return_value=labels):
            self.assertEqual(doi_provenance.get_build_provenance("demo_1.0_20260101")["revision"], revision)
        labels["org.opencontainers.image.title"] = "wrong"
        with patch.object(doi_provenance.RegistryReader, "image_labels", return_value=labels):
            with self.assertRaises(doi_license.LicenseValidationError):
                doi_provenance.get_build_provenance("demo_1.0_20260101")

    def test_catalog_license_mismatch_blocks_publication(self):
        artifacts = {"demo_1.0_20260101": {"doi": "", "license": ["Apache-2.0"]}}
        expected = {"zenodo_id": "mit", "entries": [{"id": "mit", "name": "MIT", "url": None}]}
        with (patch("scripts.zenodo_catalog.published_depositions", return_value={}),
              patch("scripts.zenodo_catalog.get_license", return_value=expected),
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            with self.assertRaisesRegex(ValueError, "Catalog licenses"):
                publish_batch(artifacts, ["demo_1.0_20260101"], MagicMock(), catalogue(), "token")
        publish.assert_not_called()

    def test_audit_flags_zenodo_and_apps_json_mismatches(self):
        record = {"id": 12, "title": "demo_1.0_20260101", "metadata": {"license": "apache-2.0"}}
        row = audit_deposition(record, {"demo_1.0_20260101": {"license": ["MIT"]}})
        self.assertEqual(row["status"], "mismatch")
        self.assertTrue(any("apache-2.0" in p and "mit" in p for p in row["problems"]))

    def test_audit_verified_when_licenses_match(self):
        record = {"id": 13, "title": "demo_1.0_20260101", "metadata": {"license": "mit"}}
        row = audit_deposition(record, {"demo_1.0_20260101": {"license": ["MIT"]}})
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["problems"], [])

    def test_audit_unmatched_when_title_absent_from_catalog(self):
        record = {"id": 14, "title": "demo_1.0_20260101", "metadata": {"license": "mit"}}
        row = audit_deposition(record, {})
        self.assertEqual(row["status"], "unmatched")


if __name__ == "__main__":
    unittest.main()
