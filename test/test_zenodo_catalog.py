"""DOI publication uses the domain catalog's exact image identities."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.zenodo_catalog import (
    catalog_artifacts,
    missing_batches,
    published_depositions,
    publish_deposition,
)


class ZenodoCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "domains.json").write_text(json.dumps({"domains": {
            "neuroimaging": {"source_repositories": ["NeuroDesk/neurocontainers"]},
            "microscopy": {"source_repositories": ["Example/microcontainers"]},
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
        self.assertEqual(artifacts["demo_1.0_20260101"]["source_repositories"],
                         ["NeuroDesk/neurocontainers"])
        self.assertEqual(artifacts["viewer_2.0_20261010"]["source_repositories"],
                         ["Example/microcontainers"])
        self.assertEqual(missing_batches(artifacts, {}),
                         [["viewer_2.0_20261010"], ["demo_1.0_20260101"]])

    def test_every_missing_image_is_split_across_at_most_six_jobs(self) -> None:
        artifacts = {f"tool_1.0_202601{day:02d}": {"doi": ""} for day in range(1, 15)}
        artifacts["done_1.0_20260120"] = {"doi": "10.5281/zenodo.9"}
        batches = missing_batches(artifacts, {"tool_1.0_20260114": {}})
        self.assertEqual(len(batches), 6)
        self.assertEqual(sorted(sum(batches, [])),
                         sorted(f"tool_1.0_202601{day:02d}" for day in range(1, 14)))
        self.assertEqual([len(batch) for batch in batches], [3, 2, 2, 2, 2, 2])
        self.assertEqual([batch[0] for batch in batches],
                         [f"tool_1.0_202601{day:02d}" for day in range(13, 7, -1)])
        self.assertEqual(missing_batches(artifacts, {}, jobs=20)[0], ["tool_1.0_20260114"])
        with self.assertRaises(ValueError):
            missing_batches(artifacts, {}, jobs=0)

    def test_removed_releases_are_absent_from_doi_plan(self) -> None:
        for domain in ("neuroimaging", "microscopy"):
            (self.root / domain / "apps.json").write_text("{}\n")
        artifacts = catalog_artifacts(self.root)
        self.assertEqual(artifacts, {})
        self.assertEqual(missing_batches(artifacts, {}), [[]])

    def test_launcher_alias_uses_its_image_for_doi_identity(self) -> None:
        path = self.root / "neuroimaging/apps.json"
        catalog = json.loads(path.read_text())
        catalog["demo"]["apps"]["VoxTell-demo 1.0"] = {
            "image": "demo_1.0", "version": "20260101", "license": ["MIT"],
        }
        path.write_text(json.dumps(catalog))
        artifacts = catalog_artifacts(self.root)
        self.assertEqual(set(artifacts), {"demo_1.0_20260101", "viewer_2.0_20261010"})

    def test_app_without_image_is_rejected(self) -> None:
        path = self.root / "neuroimaging/apps.json"
        catalog = json.loads(path.read_text())
        catalog["demo"]["apps"]["VoxTell-demo 1.0"] = {
            "version": "20260101", "license": ["MIT"],
        }
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, "Invalid catalog image: None"):
            catalog_artifacts(self.root)

    def test_sandbox_doi_does_not_prevent_production_publication(self) -> None:
        artifacts = catalog_artifacts(self.root)
        artifacts["demo_1.0_20260101"]["doi"] = "https://doi.org/10.5072/zenodo.1"
        artifacts["viewer_2.0_20261010"]["doi"] = "https://doi.org/10.5281/zenodo.2"

        self.assertEqual(missing_batches(artifacts, {}), [["demo_1.0_20260101"]])

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
                "source_repository": "Example/microcontainers",
            }, session)

        self.assertEqual(doi, "10.5281/zenodo.12")
        self.assertEqual(get.call_args.kwargs, {
            "stream": True, "timeout": (15, 300)
        })
        download.close.assert_called_once()
        metadata = session.put.call_args_list[1].kwargs["json"]["metadata"]
        self.assertEqual(metadata["license"], "mit")
        self.assertIn("Build recipe:", metadata["description"])
        self.assertIn("github.com/Example/microcontainers", metadata["description"])

    def test_published_lookup_limits_titles_and_checks_exact_match(self) -> None:
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = [
            {"title": "demo_1.0_20260101", "doi": "10.123/demo"},
            {"title": "demo_1.0_20260101-extra", "doi": "10.123/wrong"},
        ]
        session.get.return_value = response
        rows = published_depositions(session, {"demo_1.0_20260101"})
        self.assertEqual(set(rows), {"demo_1.0_20260101"})
        self.assertEqual(session.get.call_args.kwargs["params"]["q"],
                         'metadata.title:"demo_1.0_20260101"')


if __name__ == "__main__":
    unittest.main()
