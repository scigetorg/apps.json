"""DOI publication uses the domain catalog's exact image identities."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.zenodo_catalog import (
    catalog_artifacts,
    missing_batches,
    publish_deposition,
)


class ZenodoCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "domains.json").write_text(json.dumps({"domains": {
            "neuroimaging": {}, "microscopy": {},
        }}))
        for domain in ("neuroimaging", "microscopy"):
            (self.root / domain).mkdir()
        (self.root / "neuroimaging/apps.json").write_text(json.dumps({
            "demo": {"apps": {
                "demo 1.0": {"image": "demo_1.0", "version": "20260101",
                             "license": ["MIT"]},
                "viewerGUI-demo 1.0": {"image": "demo_1.0", "version": "20260101"},
            }, "categories": ["visualization"]},
        }))
        (self.root / "microscopy/apps.json").write_text(json.dumps({
            "viewer": {"apps": {
                "viewerGUI-viewer 2.0": {"image": "viewer_2.0",
                                                "version": "20261010"},
            }, "categories": ["visualization"]},
        }))

    def test_plans_unique_images_including_gui_only_release(self) -> None:
        artifacts = catalog_artifacts(self.root)
        self.assertEqual(set(artifacts), {
            "demo_1.0_20260101", "viewer_2.0_20261010"
        })
        self.assertEqual(artifacts["demo_1.0_20260101"]["license"], ["MIT"])
        self.assertEqual(missing_batches(artifacts, {}, 1), [["viewer_2.0_20261010"]])

    def test_sandbox_doi_does_not_prevent_production_publication(self) -> None:
        artifacts = catalog_artifacts(self.root)
        artifacts["demo_1.0_20260101"]["doi"] = "https://doi.org/10.5072/zenodo.1"
        artifacts["viewer_2.0_20261010"]["doi"] = "https://doi.org/10.5281/zenodo.2"

        self.assertEqual(missing_batches(artifacts, {}, 16), [["demo_1.0_20260101"]])

    def test_public_sif_download_has_no_zenodo_token(self) -> None:
        download = MagicMock()
        download.headers = {"Content-Length": "3"}
        download.raw.read.return_value = b"sif"
        created = MagicMock()
        created.json.return_value = {"id": 12, "links": {
            "bucket": "https://zenodo.org/api/files/bucket"
        }}
        published = MagicMock()
        published.json.return_value = {"doi": "10.5281/zenodo.12"}
        session = MagicMock()
        session.post.side_effect = [created, published]
        session.put.return_value = MagicMock()

        with patch("scripts.zenodo_catalog.requests.get", return_value=download) as get:
            doi = publish_deposition("demo_1.0_20260101", {
                "zenodo_id": "mit", "entries": [{"name": "MIT", "url": None}],
                "revision": "a" * 40, "recipe": "demo",
            }, session)

        self.assertEqual(doi, "10.5281/zenodo.12")
        self.assertEqual(get.call_args.kwargs, {
            "stream": True, "timeout": (15, 300)
        })
        download.close.assert_called_once()
        metadata = session.put.call_args_list[1].kwargs["json"]["metadata"]
        self.assertEqual(metadata["license"], "mit")
        self.assertIn("Build recipe:", metadata["description"])


if __name__ == "__main__":
    unittest.main()
