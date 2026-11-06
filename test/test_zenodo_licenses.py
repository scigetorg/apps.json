"""License list published for neurocontainers-ui: Zenodo IDs the builder also accepts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import zenodo_licenses
from scripts.doi_license import CatalogueError


def entry(license_id, title=None):
    return {"id": license_id, "title": title or license_id, "url": f"https://example.org/{license_id}",
            "osi_approved": True}


SPDX = {
    "mit": {"id": "MIT", "deprecated": False},
    "gpl-3.0-only": {"id": "GPL-3.0-only", "deprecated": False},
    "gpl-3.0-or-later": {"id": "GPL-3.0-or-later", "deprecated": False},
    "gpl-3.0": {"id": "GPL-3.0", "deprecated": True},
    "bsd-2-clause-freebsd": {"id": "BSD-2-Clause-FreeBSD", "deprecated": False},
}
ENTRIES = {key: entry(key) for key in ("other-open", "other-closed", "ukcrown", "gpl-3.0-or-later",
                                        "mit", "gpl-3.0-only", "gpl-3.0")}


class LicenseListTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(zenodo_licenses, "MINIMUM_LICENSES", 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lists_zenodo_ids_the_builder_accepts_in_spdx_casing(self):
        document = zenodo_licenses.build_license_list(ENTRIES, SPDX, "26.3")
        # Zenodo-only (other-*, ukcrown), SPDX-only (FreeBSD) and deprecated IDs are left out.
        self.assertEqual([row["id"] for row in document["licenses"]],
                         ["GPL-3.0-only", "GPL-3.0-or-later", "MIT"])
        self.assertEqual(document["licenses"][2],
                         {"id": "MIT", "title": "mit", "url": "https://example.org/mit", "osi_approved": True})
        self.assertEqual(document["source"]["packaging"], "26.3")

    def test_deprecated_aliases_point_at_listed_ids(self):
        document = zenodo_licenses.build_license_list(ENTRIES, SPDX, "26.3")
        self.assertEqual(document["deprecated_aliases"],
                         {"GPL-3.0": "GPL-3.0-only", "GPL-3.0+": "GPL-3.0-or-later"})

    def test_truncated_catalogue_is_refused(self):
        with patch.object(zenodo_licenses, "MINIMUM_LICENSES", 4):
            with self.assertRaisesRegex(CatalogueError, "expected at least 4"):
                zenodo_licenses.build_license_list(ENTRIES, SPDX, "26.3")

    def test_failed_fetch_keeps_the_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "licenses" / "zenodo.json"
            output.parent.mkdir()
            output.write_text('{"licenses": ["kept"]}\n', encoding="utf-8")
            with patch.object(zenodo_licenses, "fetch_catalogue", side_effect=CatalogueError("down")):
                self.assertEqual(zenodo_licenses.main(["--output", str(output)]), 1)
            self.assertEqual(json.loads(output.read_text()), {"licenses": ["kept"]})
            self.assertEqual([p.name for p in output.parent.iterdir()], ["zenodo.json"])

    def test_output_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "zenodo.json"
            with patch.object(zenodo_licenses, "fetch_catalogue", return_value={"entries": ENTRIES}), \
                 patch.object(zenodo_licenses, "LICENSES", SPDX):
                self.assertEqual(zenodo_licenses.main(["--output", str(output)]), 0)
                first = output.read_bytes()
                reordered = dict(reversed(list(ENTRIES.items())))
                with patch.object(zenodo_licenses, "fetch_catalogue", return_value={"entries": reordered}):
                    zenodo_licenses.main(["--output", str(output)])
            self.assertEqual(output.read_bytes(), first)


if __name__ == "__main__":
    unittest.main()
