"""Build-time license validation and published DOI audit contracts."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import audit_doi_licenses, catalog_licenses, doi_license, doi_metadata, doi_provenance
from scripts.audit_doi_licenses import audit_deposition, resolve_published_licenses
from scripts.license_terms import declared_licenses
from scripts.zenodo_catalog import publish_batch


def catalogue():
    return {
        "mit": {"id": "mit", "title": "MIT License", "url": "https://spdx.org/licenses/MIT.html", "osi_approved": True},
        "apache-2.0": {"id": "apache-2.0", "title": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0", "osi_approved": True},
        "other-open": {"id": "other-open", "title": "Other Open", "url": None, "osi_approved": False},
        "other-closed": {"id": "other-closed", "title": "Other Closed", "url": None, "osi_approved": False},
    }


def resolved(title="demo_1.0_20260101", declared=None, zenodo_id="mit"):
    return {title: {"revision": "a" * 40, "declared": declared or ["MIT"],
                    "license_details": [], "zenodo_id": zenodo_id}}


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

    def test_multiple_zenodo_ids_use_separate_copyright_entries(self):
        info = doi_license.resolve_recipe_license({"copyright": [
            {"license": "MIT"}, {"license": "Apache-2.0"}
        ]}, catalogue())
        self.assertEqual(info["zenodo_id"], "other-open")
        self.assertEqual([entry["id"] for entry in info["entries"]],
                         ["mit", "apache-2.0"])
        with self.assertRaisesRegex(doi_license.LicenseValidationError,
                                    "not a Zenodo catalogue ID"):
            doi_license.resolve_recipe_license(
                {"copyright": [{"license": "MIT OR Apache-2.0"}]}, catalogue()
            )

    def test_recipe_can_select_custom_zenodo_category(self):
        recipe = {"copyright": [{"name": "Research terms",
                                  "url": "https://example.org/terms"}]}
        with self.assertRaisesRegex(doi_license.LicenseValidationError,
                                    "needs zenodo_license"):
            doi_license.resolve_recipe_license(recipe, catalogue())
        info = doi_license.resolve_recipe_license(
            {**recipe, "zenodo_license": "other-closed"}, catalogue()
        )
        self.assertEqual(info["zenodo_id"], "other-closed")
        with self.assertRaisesRegex(doi_license.LicenseValidationError, "conflicts"):
            doi_license.resolve_recipe_license({**recipe,
                "copyright": [{**recipe["copyright"][0], "zenodo_license": "other-open"}],
                "zenodo_license": "other-closed"}, catalogue())

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
        self.assertEqual(info["declared"], ["MIT"])

    def test_catalog_resolver_keeps_original_custom_terms(self):
        recipe = {"copyright": [{"license": "other-closed", "name": "Research terms",
                                "url": "https://example.org/terms"}]}
        with patch.object(catalog_licenses, "get_build_recipe", return_value=(
            recipe, {"revision": "a" * 40, "recipe": "demo"}
        )):
            result = catalog_licenses.resolve_one("demo_1.0_20260101", "token")
        self.assertEqual(result["license"], ["other-closed"])
        self.assertEqual(result["license_details"], recipe["copyright"])
        self.assertEqual(declared_licenses({"copyright": [{"name": "Research terms",
            "url": "https://example.org/terms"}]}), ["Research terms"])

    def test_audit_reads_each_published_build_recipe(self):
        titles = {"demo_1.0_20260101", "viewer_2.0_20260102"}
        with patch("scripts.audit_doi_licenses.get_license", side_effect=lambda title, *_:
                   resolved(title)[title]) as lookup:
            results = resolve_published_licenses(titles, "token", catalogue())
        self.assertEqual(set(results), titles)
        self.assertEqual(lookup.call_count, 2)

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
        expected = {"zenodo_id": "mit", "declared": ["MIT"], "license_details": [],
                    "entries": [{"id": "mit", "name": "MIT", "url": None}]}
        with (patch("scripts.zenodo_catalog.published_depositions", return_value={}),
              patch("scripts.zenodo_catalog.get_license", return_value=expected),
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            with self.assertRaisesRegex(ValueError, "Catalog licenses"):
                publish_batch(artifacts, ["demo_1.0_20260101"], MagicMock(), catalogue(), "token")
        publish.assert_not_called()

    def test_custom_license_name_can_publish_with_zenodo_category(self):
        title = "demo_1.0_20260101"
        details = [{"name": "Research terms", "url": "https://example.org/terms"}]
        artifacts = {title: {"doi": "", "license": ["Research terms"],
                             "license_details": details}}
        expected = {"zenodo_id": "other-closed", "declared": ["Research terms"],
                    "license_details": details,
                    "entries": [{"id": "other-closed", "name": "Research terms",
                                 "url": "https://example.org/terms"}]}
        with (patch("scripts.zenodo_catalog.published_depositions", return_value={}),
              patch("scripts.zenodo_catalog.get_license", return_value=expected),
              patch("scripts.zenodo_catalog.publish_deposition",
                    return_value="10.123/demo") as publish):
            count = publish_batch(artifacts, [title], MagicMock(), catalogue(), "token")
        self.assertEqual(count, 1)
        publish.assert_called_once()

    def test_publication_skips_record_published_since_planning(self):
        title = "demo_1.0_20260101"
        artifacts = {title: {"doi": "", "license": ["MIT"]}}
        with (patch("scripts.zenodo_catalog.published_depositions",
                    return_value={title: {"doi": "10.123/demo"}}) as lookup,
              patch("scripts.zenodo_catalog.get_license") as get_license,
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            count = publish_batch(artifacts, [title], MagicMock(), catalogue(), "token")
        self.assertEqual(count, 0)
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[1], {title})
        get_license.assert_not_called()
        publish.assert_not_called()

    def test_audit_flags_zenodo_and_apps_json_mismatches(self):
        record = {"id": 12, "title": "demo_1.0_20260101", "metadata": {"license": "apache-2.0"}}
        row = audit_deposition(record, {"demo_1.0_20260101": {"license": ["MIT"]}},
                               resolved())
        self.assertEqual(row["status"], "mismatch")
        self.assertTrue(any("apache-2.0" in p and "mit" in p for p in row["problems"]))

    def test_audit_verified_when_licenses_match(self):
        record = {"id": 13, "title": "demo_1.0_20260101", "metadata": {"license": "mit"}}
        row = audit_deposition(record, {"demo_1.0_20260101": {"license": ["MIT"]}},
                               resolved())
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["problems"], [])

    def test_audit_accepts_zenodo_license_alias(self):
        title = "demo_1.0_20260101"
        record = {"id": 19, "title": title, "metadata": {"license": {"id": "mit-license"}}}
        row = audit_deposition(record, {title: {"license": ["MIT"]}},
                               resolved())
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["published_license"], "mit-license")
        self.assertEqual(row["expected_zenodo_license"], "mit")

    def test_audit_checks_historical_build_absent_from_catalog(self):
        record = {"id": 14, "title": "demo_1.0_20260101", "metadata": {"license": "mit"}}
        row = audit_deposition(record, {}, resolved())
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["catalog_status"], "absent")
        self.assertNotIn("catalog_license", row)

    def test_audit_flags_historical_zenodo_license_mismatch(self):
        record = {"id": 20, "title": "demo_1.0_20260101", "metadata": {"license": "mit"}}
        row = audit_deposition(record, {}, resolved(zenodo_id="other-open"))
        self.assertEqual(row["status"], "mismatch")
        self.assertEqual(len(row["problems"]), 1)
        self.assertIn("Zenodo license", row["problems"][0])

    def test_missing_historical_build_is_unverified(self):
        record = {"id": 22, "title": "demo_1.0_20260101", "metadata": {"license": "mit"}}
        row = audit_deposition(record, {}, {record["title"]: RuntimeError("Image unavailable")})
        self.assertEqual(row["status"], "unverified")
        self.assertIn("Image unavailable", row["reason"])

    def test_audit_omits_published_images_absent_from_catalog(self):
        old = "demo_1.0_20260101"
        current = "demo_1.0_20260201"
        depositions = [
            {"id": 23, "title": old, "metadata": {"upload_type": "software", "license": "mit"}},
            {"id": 24, "title": current, "metadata": {"upload_type": "software", "license": "mit"}},
            {"id": 25, "title": "invalid", "metadata": {"upload_type": "software", "license": "mit"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            with (patch("sys.argv", ["audit", "--output", str(output)]),
                  patch.dict("os.environ", {"ZENODO_TOKEN": "token"}),
                  patch.object(audit_doi_licenses, "catalog_artifacts",
                               return_value={current: {"license": ["MIT"]}}),
                  patch.object(audit_doi_licenses, "fetch_catalogue",
                               return_value={"entries": catalogue()}),
                  patch.object(audit_doi_licenses, "fetch_depositions",
                               return_value=depositions),
                  patch.object(audit_doi_licenses, "resolve_published_licenses",
                               return_value=resolved(current)) as lookup):
                self.assertEqual(audit_doi_licenses.main(), 0)
            self.assertEqual(lookup.call_args.args[0], {current})
            report = json.loads(output.read_text())
            rows = {row["title"]: row for row in report["records"]}
            self.assertEqual(set(rows), {current})
            self.assertEqual(report["summary"], {"verified": 1})

    def test_audit_reports_unavailable_build_recipe(self):
        title = "demo_1.0_20260101"
        record = {"id": 21, "title": title, "metadata": {"license": "mit"}}
        row = audit_deposition(record, {title: {"license": ["MIT"]}},
                               {title: RuntimeError("Registry unavailable")})
        self.assertEqual(row["status"], "unverified")
        self.assertIn("Registry unavailable", row["reason"])

    def test_audit_requires_exact_multi_license_zenodo_category(self):
        title = "demo_1.0_20260101"
        record = {"id": 15, "title": title, "metadata": {"license": "other-closed"}}
        artifacts = {title: {"license": ["MIT", "Apache-2.0"]}}
        row = audit_deposition(record, artifacts,
                               resolved(title, ["MIT", "Apache-2.0"], "other-open"))
        self.assertEqual(row["status"], "mismatch")
        self.assertEqual(row["expected_zenodo_license"], "other-open")

    def test_audit_detects_catalog_drift_even_when_zenodo_matches(self):
        title = "demo_1.0_20260101"
        record = {"id": 16, "title": title, "metadata": {"license": "mit"}}
        artifacts = {title: {"license": ["Apache-2.0"]}}
        row = audit_deposition(record, artifacts, resolved())
        self.assertEqual(row["status"], "mismatch")
        self.assertTrue(any("apps.json" in problem for problem in row["problems"]))

    def test_audit_keeps_recipe_zenodo_category(self):
        title = "demo_1.0_20260101"
        record = {"id": 18, "title": title, "metadata": {"license": "other-closed"}}
        artifacts = {title: {"license": ["MIT", "Apache-2.0"]}}
        row = audit_deposition(record, artifacts,
                               resolved(title, ["MIT", "Apache-2.0"], "other-closed"))
        self.assertEqual(row["status"], "verified")

if __name__ == "__main__":
    unittest.main()
