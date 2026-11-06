"""Build-time license validation and published DOI audit contracts."""

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import audit_doi_licenses, doi_license, zenodo_catalog
from scripts.audit_doi_licenses import audit_record
from scripts.license_terms import declared_licenses, license_details
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


def recipes(result=None, side_effect=None):
    """Stub SourceRecipes: `license(title, repositories, catalogue)` from recipe history."""
    stub = MagicMock()
    stub.license.return_value = {"version": "1.0", "revision": "a" * 40, **(result or {})}
    stub.license.side_effect = side_effect
    return stub


DETAILS = [{"name": "Research terms", "url": "https://example.org/terms"}]


def record(rights, title=TITLE, resource_type="software"):
    """A published record as /api/user/records returns it (InvenioRDM JSON)."""
    return {"id": "12", "is_published": True, "pids": {"doi": {"identifier": "10.5281/zenodo.12"}},
            "metadata": {"title": title, "rights": rights, "resource_type": {"id": resource_type}}}


class DoiLicenseTests(unittest.TestCase):
    def test_catalogue_ids_and_custom_terms_become_rights(self):
        info = doi_license.resolve_recipe_license(
            {"copyright": [{"license": "MIT"}]}, catalogue()
        )
        self.assertEqual(info["rights"], [{"id": "mit"}])
        with self.assertRaisesRegex(doi_license.LicenseValidationError, "not a Zenodo catalogue ID"):
            doi_license.resolve_recipe_license(
                {"copyright": [{"license": "MIT-Modified", "url": "https://example.org/terms"}]},
                catalogue(),
            )
        terms = {"title": {"en": "Research terms"}, "link": "https://example.org/terms"}
        for entry in ({"name": "Research terms", "url": "https://example.org/terms"},
                      {"license": "other-open", "name": "Research terms", "url": "https://example.org/terms"},
                      {"license": "LicenseRef-Research", "name": "Research terms",
                       "url": "https://example.org/terms"}):
            info = doi_license.resolve_recipe_license({"copyright": [entry]}, catalogue())
            self.assertEqual(info["rights"], [terms], entry)

    def test_deprecated_spdx_gpl_id_maps_to_zenodo_id(self):
        for stated, expected in (("GPL-3.0", "gpl-3.0-only"), ("GPL-3.0+", "gpl-3.0-or-later")):
            info = doi_license.resolve_recipe_license(
                {"copyright": [{"license": stated, "url": "https://www.gnu.org/licenses/gpl-3.0-standalone.html"}]},
                catalogue(),
            )
            self.assertEqual(info["rights"], [{"id": expected}])

    def test_multiple_licenses_publish_as_separate_rights(self):
        info = doi_license.resolve_recipe_license({"copyright": [
            {"license": "MIT"}, {"license": "Apache-2.0"}, {"license": "mit"},
            {"name": "Research terms", "url": "https://example.org/terms"},
        ]}, catalogue())
        self.assertEqual(info["rights"], [
            {"id": "mit"}, {"id": "apache-2.0"},
            {"title": {"en": "Research terms"}, "link": "https://example.org/terms"},
        ])
        with self.assertRaisesRegex(doi_license.LicenseValidationError,
                                    "not a Zenodo catalogue ID"):
            doi_license.resolve_recipe_license(
                {"copyright": [{"license": "MIT OR Apache-2.0"}]}, catalogue()
            )

    def test_custom_terms_need_name_and_url_and_zenodo_license_is_ignored(self):
        for entry, message in (({"license": "LicenseRef-PARAPET", "url": "https://example.org/p"},
                                "needs a name and URL"),
                               ({"name": "Research terms"}, "needs a URL"),
                               ({"license": "other-closed"}, "needs a name and URL"),
                               ({"name": "Unknown", "url": "https://example.org/u"},
                                "does not identify usable license terms")):
            with self.assertRaisesRegex(doi_license.LicenseValidationError, message):
                doi_license.resolve_recipe_license({"copyright": [entry]}, catalogue())
        info = doi_license.resolve_recipe_license({"zenodo_license": "other-closed", "copyright": [
            {"name": "Research terms", "url": "https://example.org/terms", "zenodo_license": "other-open"},
        ]}, catalogue())
        self.assertEqual(info["rights"], [{"title": {"en": "Research terms"},
                                           "link": "https://example.org/terms"}])

    def test_catalog_entry_rebuilds_the_recipe_rights(self):
        recipe = {"copyright": [
            {"license": "GPL-3.0"}, {"license": "MIT", "name": "MIT License"},
            {"license": "other-closed", "name": "Research terms", "url": "https://example.org/terms"},
            {"name": "Viewer terms", "url": "https://example.org/viewer"},
        ]}
        entry = {"license": declared_licenses(recipe), "license_details": license_details(recipe)}
        resolved = doi_license.resolve_recipe_license(recipe, catalogue())["rights"]
        self.assertTrue(doi_license.same_rights(resolved, doi_license.rights_from_catalog(entry)))
        expanded = [{"id": "gpl-3.0-only", "title": {"en": "GPL"}, "props": {}},
                    {"id": "mit", "title": {"en": "MIT License"}},
                    {"title": {"en": "research TERMS"}, "link": "https://example.org/terms/"},
                    {"title": {"en": "Viewer terms"}, "link": "https://example.org/viewer"}]
        self.assertTrue(doi_license.same_rights(expanded, resolved))
        self.assertFalse(doi_license.same_rights(expanded[:3], resolved))
        with self.assertRaisesRegex(ValueError, "needs a name and URL"):
            doi_license.rights_from_catalog({"license": ["other-open"],
                                             "license_details": [{"license": "other-open"}]})

    def test_every_zenodo_other_category_needs_a_name_and_url(self):
        cats = {**catalogue(), "other-nc": {"id": "other-nc", "title": "Other (Non-Commercial)", "url": None}}
        with self.assertRaisesRegex(doi_license.LicenseValidationError, "needs a name and URL"):
            doi_license.resolve_recipe_license({"copyright": [{"license": "other-nc"}]}, cats)
        terms = {"license": "other-nc", "name": "Research terms", "url": "https://example.org/terms"}
        self.assertEqual(doi_license.resolve_recipe_license({"copyright": [terms]}, cats)["rights"],
                         [{"title": {"en": "Research terms"}, "link": "https://example.org/terms"}])
        self.assertEqual(declared_licenses({"copyright": [terms]}), ["Research terms"])

    def test_custom_terms_are_listed_by_name_in_apps_json(self):
        terms = {"license": "other-closed", "name": "FSL license", "url": "https://example.org/fsl"}
        recipe = {"copyright": [{"license": "MIT", "name": "MIT License"}, terms,
                                {"license": "LicenseRef-X", "url": "https://example.org/x"},
                                {"name": "Viewer terms", "url": "https://example.org/viewer"}]}
        self.assertEqual(declared_licenses(recipe),
                         ["FSL license", "LicenseRef-X", "MIT", "Viewer terms"])
        # Entries written before this change list the ID instead; both give the same rights.
        details = [terms]
        expected = [{"id": "mit"}, {"title": {"en": "FSL license"}, "link": "https://example.org/fsl"}]
        for listed in (["FSL license", "MIT"], ["MIT", "other-closed"]):
            self.assertEqual(doi_license.rights_from_catalog(
                {"license": listed, "license_details": details}), expected, listed)

    def test_custom_catalog_license_without_details_is_refused(self):
        # Entries written before license_details existed: never turn them into IDs.
        for value in ("other-closed", "LicenseRef-FSL", "FreeSurfer Software License Agreement"):
            with self.assertRaisesRegex(ValueError, "no license_details"):
                doi_license.rights_from_catalog({"license": ["MIT", value]})
        # A free-text license value is custom too, so its details are kept.
        recipe = {"copyright": [{"license": "FreeSurfer Software License Agreement",
                                 "url": "https://example.org/fs"}, {"license": "MIT"}]}
        self.assertEqual(license_details(recipe), [{"license": "FreeSurfer Software License Agreement",
                                                    "url": "https://example.org/fs"}])
        with self.assertRaisesRegex(ValueError, "needs a name and URL"):
            doi_license.rights_from_catalog({"license": declared_licenses(recipe),
                                             "license_details": license_details(recipe)})
        row = audit_record(record([{"id": "other-closed"}]), {"license": ["other-closed"]})
        self.assertEqual(row["status"], "mismatch")
        self.assertIn("no license_details", row["problems"][0])

    def test_catalog_license_mismatch_blocks_publication(self):
        artifacts = {"demo_1.0_20260101": {"doi": "", "license": ["Apache-2.0"],
                                           "source_repositories": ["Example/microcontainers"]}}
        expected = {"rights": [{"id": "mit"}], "declared": ["MIT"], "license_details": []}
        with (patch("scripts.zenodo_catalog.user_records", return_value={}),
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            with self.assertRaisesRegex(RuntimeError, "demo_1.0_20260101: ValueError: Catalog licenses"
                                        r".*recipe for version 1\.0"):
                publish_batch(artifacts, ["demo_1.0_20260101"], MagicMock(), catalogue(),
                              recipes(expected))
        publish.assert_not_called()

    def test_license_failure_names_image_and_batch_continues(self):
        artifacts = {"bad_1.0_20260101": {"doi": "", "license": ["Odd"], "source_repositories": []},
                     "good_1.0_20260101": {"doi": "", "license": ["MIT"], "source_repositories": []}}
        good = {"rights": [{"id": "mit"}], "declared": ["MIT"], "license_details": [],
                "version": "1.0", "revision": "a" * 40}

        def get_license(title, *_):
            if title.startswith("bad"):
                raise doi_license.LicenseValidationError("not a Zenodo catalogue ID")
            return good

        with (patch("scripts.zenodo_catalog.user_records", return_value={}),
              patch("scripts.zenodo_catalog.publish_deposition",
                    return_value="10.123/good") as publish):
            with self.assertRaisesRegex(
                RuntimeError, r"(?s)Published 1; .*bad_1\.0_20260101: LicenseValidationError"
            ):
                publish_batch(artifacts, list(artifacts), MagicMock(), catalogue(),
                              recipes(side_effect=get_license))
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], "good_1.0_20260101")

    def test_custom_license_name_publishes_as_titled_right(self):
        title = "demo_1.0_20260101"
        details = [{"name": "Research terms", "url": "https://example.org/terms"}]
        artifacts = {title: {"doi": "", "license": ["Research terms"],
                             "license_details": details,
                             "source_repositories": ["Example/microcontainers"]}}
        expected = {"rights": [{"title": {"en": "Research terms"}, "link": "https://example.org/terms"}],
                    "declared": ["Research terms"], "license_details": details}
        lookup = recipes(expected)
        with (patch("scripts.zenodo_catalog.user_records", return_value={}),
              patch("scripts.zenodo_catalog.publish_deposition",
                    return_value="10.123/demo") as publish):
            count = publish_batch(artifacts, [title], MagicMock(), catalogue(), lookup)
        self.assertEqual(count, 1)
        lookup.license.assert_called_once_with(title, ["Example/microcontainers"], catalogue())
        publish.assert_called_once()

    def test_publication_skips_record_published_since_planning(self):
        title = "demo_1.0_20260101"
        artifacts = {title: {"doi": "", "license": ["MIT"]}}
        with (patch("scripts.zenodo_catalog.user_records",
                    return_value={title: {"pids": {"doi": {"identifier": "10.123/demo"}}}}) as lookup,
              patch("scripts.zenodo_catalog.publish_deposition") as publish):
            resolve_license = recipes()
            count = publish_batch(artifacts, [title], MagicMock(), catalogue(), resolve_license)
        self.assertEqual(count, 0)
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[1], {title})
        resolve_license.license.assert_not_called()
        publish.assert_not_called()

    def test_publication_stops_starting_uploads_after_the_time_budget(self):
        artifacts = {title: {"doi": "", "license": ["MIT"], "source_repositories": []}
                     for title in ("a_1.0_20260101", "b_1.0_20260101")}
        with (patch("scripts.zenodo_catalog.user_records", return_value={}),
              patch("scripts.zenodo_catalog.publish_deposition", return_value="10.5281/zenodo.1") as publish,
              patch("scripts.zenodo_catalog.time.monotonic", side_effect=[0, 61]),
              patch("sys.stdout")):
            count = publish_batch(artifacts, list(artifacts), MagicMock(), catalogue(),
                                  recipes({"rights": [{"id": "mit"}]}), deadline=60)
        self.assertEqual(count, 1)
        publish.assert_called_once()

    def test_license_equivalence_folds_legacy_ids_without_cycles(self):
        key = doi_license.license_equivalence_key
        for left, right in (("gpl-3.0", "gpl-3.0-only"), ("GPL-2.0", "gpl-2.0-only"),
                            ("mit-license", "mit"), ("apache2.0", "Apache-2.0"),
                            ("apgl-v3", "agpl-3.0-only"), ("lgpl-2.1+", "lgpl-2.1-or-later"),
                            ("bsd-2-clause-netbsd", "bsd-2-clause")):
            self.assertEqual(key(left), key(right), (left, right))
        self.assertNotEqual(key("gpl-3.0-only"), key("gpl-3.0-or-later"))
        self.assertNotEqual(key("mit"), key("apache-2.0"))

    def test_records_api_defaults_to_production_zenodo(self):
        with patch.dict("os.environ", {}, clear=True):
            module = importlib.reload(zenodo_catalog)
        self.addCleanup(importlib.reload, zenodo_catalog)
        self.assertEqual(module.ZENODO_API, "https://zenodo.org/api")

    def test_audit_verifies_matching_rights_including_legacy_ids(self):
        for published, entry in (
            ([{"id": "mit", "title": {"en": "MIT License"}}], {"license": ["MIT"]}),
            ([{"id": "gpl-3.0-only"}], {"license": ["GPL-3.0"]}),
            ([{"id": "apache-2.0"}, {"id": "mit"}], {"license": ["Apache-2.0", "MIT"]}),
            ([{"title": {"en": "Research terms"}, "link": "https://example.org/terms"}],
             {"license": ["Research terms"], "license_details": DETAILS}),
        ):
            row = audit_record(record(published), entry)
            self.assertEqual(row["status"], "verified", published)
            self.assertEqual(row["problems"], [])
        self.assertEqual(row["doi_url"], "https://doi.org/10.5281/zenodo.12")

    def test_audit_accepts_single_other_category_from_legacy_publication(self):
        for entry in ({"license": ["Apache-2.0", "MIT"]},
                      {"license": ["Research terms"], "license_details": DETAILS}):
            for other in ("other-open", "other-closed", "other-at", "other-nc", "other-pd"):
                row = audit_record(record([{"id": other}]), entry)
                self.assertEqual(row["status"], "verified_legacy", (entry, other))
        row = audit_record(record([{"id": "other-open"}]), {"license": ["MIT"]})
        self.assertEqual(row["status"], "mismatch")
        # The legacy API also kept one real license of several (dsistudio).
        entry = {"license": ["CC-BY-NC-SA-4.0", "LGPL-3.0"]}
        row = audit_record(record([{"id": "cc-by-nc-sa-4.0"}]), entry)
        self.assertEqual(row["status"], "verified_legacy")
        # A single license apps.json does not list is a real disagreement.
        row = audit_record(record([{"id": "mit"}]), entry)
        self.assertEqual(row["status"], "mismatch")

    def test_audit_flags_rights_that_differ_from_apps_json(self):
        row = audit_record(record([{"id": "apache-2.0"}]), {"license": ["MIT"]})
        self.assertEqual(row["status"], "mismatch")
        self.assertEqual(row["catalog_license"], ["mit"])
        self.assertIn("apache-2.0", row["problems"][0])
        row = audit_record(record([{"id": "apache-2.0"}, {"id": "gpl-3.0-only"}]),
                           {"license": ["Apache-2.0", "MIT"]})
        self.assertEqual(row["status"], "mismatch")
        row = audit_record(record([{"id": "other-open"}]), {
            "license": ["other-open"], "license_details": [{"license": "other-open"}]})
        self.assertEqual(row["status"], "mismatch")
        self.assertIn("incomplete", row["problems"][0])

    def test_audit_reports_catalog_entry_without_license(self):
        row = audit_record(record([{"id": "mit"}]), {"license": []})
        self.assertEqual(row["status"], "no_catalog_license")

    def test_audit_skips_records_absent_from_catalog_and_non_software(self):
        current = "demo_1.0_20260201"
        records = {
            TITLE: record([{"id": "mit"}]),
            current: record([{"id": "apache-2.0"}], title=current),
            "dataset_1.0_20260201": record([{"id": "mit"}], title="dataset_1.0_20260201",
                                           resource_type="dataset"),
        }
        artifacts = {current: {"license": ["MIT"]}, "dataset_1.0_20260201": {"license": ["MIT"]}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            with (patch("sys.argv", ["audit", "--output", str(output)]),
                  patch.dict("os.environ", {"ZENODO_TOKEN": "token"}, clear=True),
                  patch.object(audit_doi_licenses, "catalog_artifacts", return_value=artifacts),
                  patch.object(audit_doi_licenses, "user_records", return_value=records)):
                self.assertEqual(audit_doi_licenses.main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual([row["title"] for row in report["records"]], [current])
            self.assertEqual(report["summary"], {"mismatch": 1})

if __name__ == "__main__":
    unittest.main()
