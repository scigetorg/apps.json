"""Build-time license validation and published DOI audit contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import audit_doi_licenses, catalog_licenses, doi_license, doi_metadata, doi_provenance
from scripts.audit_doi_licenses import audit_deposition
from scripts.license_terms import declared_licenses
from scripts.zenodo_catalog import publish_batch


def catalogue():
    return {
        "mit": {"id": "mit", "title": "MIT License", "url": "https://spdx.org/licenses/MIT.html", "osi_approved": True},
        "apache-2.0": {"id": "apache-2.0", "title": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0", "osi_approved": True},
        "gpl-3.0-only": {"id": "gpl-3.0-only", "title": "GNU General Public License v3.0 only", "url": "https://www.gnu.org/licenses/gpl-3.0-standalone.html", "osi_approved": True},
        "gpl-3.0-or-later": {"id": "gpl-3.0-or-later", "title": "GNU General Public License v3.0 or later", "url": "https://www.gnu.org/licenses/gpl-3.0-standalone.html", "osi_approved": True},
        "other-open": {"id": "other-open", "title": "Other Open", "url": None, "osi_approved": False},
        "other-closed": {"id": "other-closed", "title": "Other Closed", "url": None, "osi_approved": False},
    }



TITLE = "demo_1.0_20260101"


def provenance(revision="a" * 40):
    return {"revision": revision, "recipe": "demo", "source_repository": "Example/microcontainers",
            "image": "ghcr.io/neurodesk/demo_1.0:20260101"}


def record(license_value, title=TITLE):
    return {"id": 12, "title": title, "metadata": {"license": license_value}}


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
        info = doi_license.resolve_recipe_license(
            {"copyright": [{"license": "other-open", "name": "Research terms",
                            "url": "https://example.org/terms"}]}, catalogue()
        )
        self.assertEqual(info["zenodo_id"], "other-open")
        self.assertEqual(info["entries"][0]["name"], "Research terms")

    def test_deprecated_spdx_gpl_id_maps_to_zenodo_id(self):
        for stated, expected in (("GPL-3.0", "gpl-3.0-only"), ("GPL-3.0+", "gpl-3.0-or-later")):
            info = doi_license.resolve_recipe_license(
                {"copyright": [{"license": stated, "url": "https://www.gnu.org/licenses/gpl-3.0-standalone.html"}]},
                catalogue(),
            )
            self.assertEqual(info["zenodo_id"], expected)
            self.assertEqual(info["entries"][0]["name"], stated)

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
        labels["org.opencontainers.image.source"] = "https://github.com/Example/microcontainers"
        labels["org.opencontainers.image.documentation"] = (
            f"https://github.com/Example/microcontainers/tree/{revision}/recipes/demo"
        )
        with patch.object(doi_provenance.RegistryReader, "image_labels", return_value=labels):
            info = doi_provenance.get_build_provenance(
                "demo_1.0_20260101", source_repositories=[
                    "NeuroDesk/neurocontainers", "Example/microcontainers"
                ]
            )
            self.assertEqual(info["source_repository"], "Example/microcontainers")
            with self.assertRaises(doi_license.LicenseValidationError):
                doi_provenance.get_build_provenance(
                    "demo_1.0_20260101", source_repositories=["Other/repo"]
                )
        labels["org.opencontainers.image.title"] = "wrong"
        with patch.object(doi_provenance.RegistryReader, "image_labels", return_value=labels):
            with self.assertRaises(doi_license.LicenseValidationError):
                doi_provenance.get_build_provenance("demo_1.0_20260101")

    def test_ghcr_token_denial_falls_back_to_docker_hub(self):
        revision = "b" * 40
        labels = {
            "org.opencontainers.image.revision": revision,
            "org.opencontainers.image.title": "demo",
            "org.opencontainers.image.version": "1.0_20260101",
            "org.opencontainers.image.source": "https://github.com/neurodesk/neurocontainers",
            "org.opencontainers.image.documentation":
                f"https://github.com/neurodesk/neurocontainers/tree/{revision}/recipes/demo",
        }

        def reply(status, payload=None, headers=None):
            return SimpleNamespace(status_code=status, ok=status < 400,
                                   headers=headers or {}, json=lambda: payload or {})

        def fake_get(token_status):
            def get(url, **kwargs):
                if url.startswith("https://ghcr.io/v2/"):
                    return reply(401, headers={"WWW-Authenticate":
                        'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:x:pull"'})
                if url == "https://ghcr.io/token":
                    return reply(token_status)
                if url.endswith("/manifests/20260101"):
                    return reply(200, {"config": {"digest": "sha256:c"}})
                if url.endswith("/blobs/sha256:c"):
                    return reply(200, {"config": {"Labels": labels}})
                raise AssertionError(url)
            return get

        with patch.object(doi_provenance.requests, "get", side_effect=fake_get(403)):
            info = doi_provenance.get_build_provenance("demo_1.0_20260101", "token")
        self.assertEqual(info["image"], "registry-1.docker.io/vnmd/demo_1.0:20260101")
        self.assertEqual(info["revision"], revision)
        # A real token-service outage is reported, not mistaken for a missing image.
        with (patch.object(doi_provenance.requests, "get", side_effect=fake_get(500)),
              self.assertRaisesRegex(doi_provenance.ProvenanceServiceError, "HTTP 500")):
            doi_provenance.get_build_provenance("demo_1.0_20260101", "token")

    def test_catalog_license_mismatch_blocks_publication(self):
        artifacts = {"demo_1.0_20260101": {"doi": "", "license": ["Apache-2.0"]}}
        expected = {"zenodo_id": "mit", "declared": ["MIT"], "license_details": [],
                    "entries": [{"id": "mit", "name": "MIT", "url": None}]}
        with (patch("scripts.zenodo_catalog.published_depositions", return_value={}),
              patch("scripts.zenodo_catalog.resolve_build_license", return_value=expected),
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            with self.assertRaisesRegex(RuntimeError, "demo_1.0_20260101: ValueError: Catalog licenses"):
                publish_batch(artifacts, ["demo_1.0_20260101"], MagicMock(), catalogue(), "token")
        publish.assert_not_called()

    def test_license_failure_names_image_and_batch_continues(self):
        artifacts = {"bad_1.0_20260101": {"doi": "", "license": ["Odd"]},
                     "good_1.0_20260101": {"doi": "", "license": ["MIT"]}}
        good = {"zenodo_id": "mit", "declared": ["MIT"], "license_details": [],
                "entries": [{"id": "mit", "name": "MIT", "url": None}]}

        def get_license(title, *_):
            if title.startswith("bad"):
                raise doi_license.LicenseValidationError("not a Zenodo catalogue ID")
            return good

        with (patch("scripts.zenodo_catalog.published_depositions", return_value={}),
              patch("scripts.zenodo_catalog.resolve_build_license", side_effect=get_license),
              patch("scripts.zenodo_catalog.publish_deposition",
                    return_value="10.123/good") as publish):
            with self.assertRaisesRegex(
                RuntimeError, r"(?s)Published 1; .*bad_1\.0_20260101: LicenseValidationError"
            ):
                publish_batch(artifacts, list(artifacts), MagicMock(), catalogue(), "token")
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], "good_1.0_20260101")

    def test_custom_license_name_can_publish_with_zenodo_category(self):
        title = "demo_1.0_20260101"
        details = [{"name": "Research terms", "url": "https://example.org/terms"}]
        artifacts = {title: {"doi": "", "license": ["Research terms"],
                             "license_details": details,
                             "source_repositories": ["Example/microcontainers"]}}
        expected = {"zenodo_id": "other-closed", "declared": ["Research terms"],
                    "license_details": details,
                    "entries": [{"id": "other-closed", "name": "Research terms",
                                 "url": "https://example.org/terms"}]}
        with (patch("scripts.zenodo_catalog.published_depositions", return_value={}),
              patch("scripts.zenodo_catalog.resolve_build_license", return_value=expected) as lookup,
              patch("scripts.zenodo_catalog.publish_deposition",
                    return_value="10.123/demo") as publish):
            count = publish_batch(artifacts, [title], MagicMock(), catalogue(), "token")
        self.assertEqual(count, 1)
        lookup.assert_called_once_with(
            title, "token", ["Example/microcontainers"], catalogue()
        )
        publish.assert_called_once()

    def test_publication_skips_record_published_since_planning(self):
        title = "demo_1.0_20260101"
        artifacts = {title: {"doi": "", "license": ["MIT"]}}
        with (patch("scripts.zenodo_catalog.published_depositions",
                    return_value={title: {"doi": "10.123/demo"}}) as lookup,
              patch("scripts.zenodo_catalog.resolve_build_license") as resolve_license,
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            count = publish_batch(artifacts, [title], MagicMock(), catalogue(), "token")
        self.assertEqual(count, 0)
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[1], {title})
        resolve_license.assert_not_called()
        publish.assert_not_called()

    def test_recipe_is_fetched_raw_at_image_build_revision(self):
        revision = "a" * 40
        response = SimpleNamespace(ok=True, status_code=200,
                                   content=json.dumps({"copyright": [{"license": "MIT"}]}).encode())
        with (patch.object(doi_metadata, "get_build_provenance", return_value=provenance(revision))
              as lookup, patch.object(doi_metadata.requests, "get", return_value=response) as get):
            info = doi_metadata.resolve_build_license(
                "demo_1.0_20260101", "token", ["Example/microcontainers"], catalogue()
            )
        lookup.assert_called_once_with("demo_1.0_20260101", "token", ["Example/microcontainers"])
        self.assertEqual(get.call_args.args[0],
                         f"https://raw.githubusercontent.com/Example/microcontainers/{revision}/"
                         "recipes/demo/build.yaml")
        self.assertEqual(info["zenodo_id"], "mit")
        self.assertEqual(info["declared"], ["MIT"])
        self.assertEqual(info["revision"], revision)

    def test_recipe_fetch_errors_are_classified(self):
        for status, error in ((404, doi_license.LicenseValidationError),
                              (500, doi_provenance.ProvenanceServiceError)):
            response = SimpleNamespace(ok=False, status_code=status, content=b"")
            with (patch.object(doi_metadata.requests, "get", return_value=response),
                  self.assertRaises(error)):
                doi_metadata.fetch_recipe("demo", "Example/microcontainers", "a" * 40)

    def test_consolidate_resolver_needs_no_zenodo_catalogue(self):
        recipe = {"copyright": [{"license": "other-closed", "name": "Research terms",
                                 "url": "https://example.org/terms"}]}
        with (patch.object(doi_metadata, "get_build_provenance", return_value=provenance()),
              patch.object(doi_metadata, "fetch_recipe", return_value=recipe),
              patch.object(doi_metadata, "resolve_recipe_license") as zenodo):
            result = catalog_licenses.resolve_one(
                "demo_1.0_20260101", "token", ["Example/microcontainers"]
            )
        zenodo.assert_not_called()
        self.assertEqual(result, {"license": ["other-closed"],
                                  "license_details": recipe["copyright"]})

    def test_missing_build_image_is_strict(self):
        with (patch.object(doi_metadata, "get_build_provenance", side_effect=
                           doi_license.LicenseValidationError("Build image is unavailable")),
              patch.object(doi_metadata, "fetch_recipe") as fetch,
              self.assertRaisesRegex(doi_license.LicenseValidationError, "unavailable")):
            doi_metadata.resolve_build_license("demo_1.0_20260101", "token", catalogue=catalogue())
        fetch.assert_not_called()

    def test_license_equivalence_folds_legacy_ids_without_cycles(self):
        key = doi_license.license_equivalence_key
        for left, right in (("gpl-3.0", "gpl-3.0-only"), ("GPL-2.0", "gpl-2.0-only"),
                            ("mit-license", "mit"), ("apache2.0", "Apache-2.0"),
                            ("apgl-v3", "agpl-3.0-only"), ("lgpl-2.1+", "lgpl-2.1-or-later"),
                            ("bsd-2-clause-netbsd", "bsd-2-clause")):
            self.assertEqual(key(left), key(right), (left, right))
        self.assertNotEqual(key("gpl-3.0-only"), key("gpl-3.0-or-later"))
        self.assertNotEqual(key("mit"), key("apache-2.0"))

    def test_audit_reads_production_zenodo(self):
        self.assertTrue(audit_doi_licenses.DEPOSITIONS_URL.startswith("https://zenodo.org/api/"))

    def test_audit_single_license_matches_including_legacy_ids(self):
        for published, catalog in (("mit", ["MIT"]), ({"id": "mit-license"}, ["MIT"]),
                                   ("gpl-3.0", ["GPL-3.0-only"])):
            row = audit_deposition(record(published), {TITLE: {"license": catalog}})
            self.assertEqual(row["status"], "verified", published)
            self.assertEqual(row["problems"], [])

    def test_audit_flags_zenodo_license_that_differs_from_apps_json(self):
        row = audit_deposition(record("apache-2.0"), {TITLE: {"license": ["MIT"]}})
        self.assertEqual(row["status"], "mismatch")
        self.assertEqual(row["catalog_license"], ["mit"])
        self.assertIn("apache-2.0", row["problems"][0])

    def test_audit_multiple_or_custom_licenses_need_other_category(self):
        details = [{"name": "Research terms", "url": "https://example.org/terms"}]
        for entry in ({"license": ["MIT", "Apache-2.0"]},
                      {"license": ["Research terms"], "license_details": details}):
            for published, status in (("other-open", "verified"), ("other-closed", "verified"),
                                      ("mit", "mismatch")):
                row = audit_deposition(record(published), {TITLE: entry})
                self.assertEqual(row["status"], status, (entry, published))
        exact = {"license": ["other-closed"], "license_details": details}
        self.assertEqual(audit_deposition(record("other-open"), {TITLE: exact})["status"], "mismatch")

    def test_audit_reports_catalog_entry_without_license(self):
        row = audit_deposition(record("mit"), {TITLE: {"license": []}})
        self.assertEqual(row["status"], "no_catalog_license")

    def test_audit_omits_published_images_absent_from_catalog(self):
        current = "demo_1.0_20260201"
        depositions = [
            {"id": 23, "title": TITLE, "metadata": {"upload_type": "software", "license": "mit"}},
            {"id": 24, "title": current, "metadata": {"upload_type": "software", "license": "apache-2.0"}},
            {"id": 25, "title": "invalid", "metadata": {"upload_type": "software", "license": "mit"}},
            {"id": 26, "title": current, "metadata": {"upload_type": "dataset", "license": "mit"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            with (patch("sys.argv", ["audit", "--output", str(output)]),
                  patch.dict("os.environ", {"ZENODO_TOKEN": "token"}, clear=True),
                  patch.object(audit_doi_licenses, "catalog_artifacts",
                               return_value={current: {"license": ["MIT"]}}),
                  patch.object(audit_doi_licenses, "fetch_depositions",
                               return_value=depositions)):
                self.assertEqual(audit_doi_licenses.main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual([row["id"] for row in report["records"]], [24])
            self.assertEqual(report["summary"], {"mismatch": 1})


if __name__ == "__main__":
    unittest.main()
