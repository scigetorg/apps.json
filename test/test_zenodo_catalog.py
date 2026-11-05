"""DOI publication uses the domain catalog's exact image identities."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from scripts.zenodo_catalog import (
    RDM_JSON,
    catalog_artifacts,
    missing_batches,
    publish_deposition,
    record_doi,
    user_records,
)

LICENSE_INFO = {
    "rights": [{"id": "mit"}, {"title": {"en": "Research terms"}, "link": "https://example.org/terms"}],
    "revision": "a" * 40, "recipe": "demo", "source_repository": "Example/microcontainers",
}


def reply(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.side_effect = (
        requests.HTTPError(str(status)) if status >= 400 else None)
    return response


def records_session(created_errors=None, **overrides):
    """Stub session answering the records API publish sequence.

    Override one step's reply by name: create, init, commit, publish or upload.
    """
    steps = {
        "create": reply({"id": "12", "errors": created_errors or []}),
        "init": reply({"entries": []}),
        "commit": reply({"size": 3}),
        "publish": reply({"id": "12", "is_published": True,
                          "pids": {"doi": {"identifier": "10.5281/zenodo.12"}}}),
        "upload": reply({"size": 3}),
        **overrides,
    }
    session = MagicMock()
    session.post.side_effect = [steps[name] for name in ("create", "init", "commit", "publish")]
    session.put.return_value = steps["upload"]
    return session


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
        # viewer has no catalog license, so it cannot be published yet.
        self.assertEqual(missing_batches(artifacts, {}), (
            [["demo_1.0_20260101"]], {"viewer_2.0_20261010": "apps.json has no license"}))

    def test_every_missing_image_is_split_across_at_most_six_jobs(self) -> None:
        artifacts = {f"tool_1.0_202601{day:02d}": {"doi": "", "license": ["MIT"]}
                     for day in range(1, 15)}
        artifacts["done_1.0_20260120"] = {"doi": "10.5281/zenodo.9", "license": ["MIT"]}
        batches, skipped = missing_batches(artifacts, {"tool_1.0_20260114": {}})
        self.assertEqual(skipped, {})
        self.assertEqual(len(batches), 6)
        self.assertEqual(sorted(sum(batches, [])),
                         sorted(f"tool_1.0_202601{day:02d}" for day in range(1, 14)))
        self.assertEqual([len(batch) for batch in batches], [3, 2, 2, 2, 2, 2])
        self.assertEqual([batch[0] for batch in batches],
                         [f"tool_1.0_202601{day:02d}" for day in range(13, 7, -1)])
        self.assertEqual(missing_batches(artifacts, {}, jobs=20)[0][0], ["tool_1.0_20260114"])
        with self.assertRaises(ValueError):
            missing_batches(artifacts, {}, jobs=0)

    def test_limit_keeps_newest_and_unlicensable_images_are_skipped(self) -> None:
        artifacts = {f"tool_1.0_202601{day:02d}": {"doi": "", "license": ["MIT"]}
                     for day in range(1, 5)}
        artifacts["fsl_1.0_20260109"] = {"doi": "", "license": ["LicenseRef-FSL"]}
        artifacts["old_1.0_20200101"] = {"doi": "", "license": []}
        batches, skipped = missing_batches(artifacts, {}, limit=1)
        self.assertEqual(batches, [["tool_1.0_20260104"]])
        self.assertEqual(sorted(skipped), ["fsl_1.0_20260109", "old_1.0_20200101"])
        self.assertIn("no license_details", skipped["fsl_1.0_20260109"])
        self.assertEqual(missing_batches({"old_1.0_20200101": artifacts["old_1.0_20200101"]}, {})[0],
                         [[]])

    def test_removed_releases_are_absent_from_doi_plan(self) -> None:
        for domain in ("neuroimaging", "microscopy"):
            (self.root / domain / "apps.json").write_text("{}\n")
        artifacts = catalog_artifacts(self.root)
        self.assertEqual(artifacts, {})
        self.assertEqual(missing_batches(artifacts, {}), ([[]], {}))

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

        self.assertEqual(missing_batches(artifacts, {})[0], [["demo_1.0_20260101"]])

    def publish(self, session, download_length="3"):
        download = MagicMock()
        download.headers = {"Content-Length": download_length}
        download.raw.read.return_value = b"sif"
        with patch("scripts.zenodo_catalog.requests.get", return_value=download) as get:
            try:
                return publish_deposition("demo_1.0_20260101", LICENSE_INFO, session)
            finally:
                self.assertEqual(get.call_args.kwargs, {"stream": True, "timeout": (15, 300)})
                self.assertNotIn("headers", get.call_args.kwargs)
                download.close.assert_called_once()

    def test_publish_creates_draft_uploads_and_publishes_through_records_api(self) -> None:
        session = records_session()
        self.assertEqual(self.publish(session), "10.5281/zenodo.12")
        draft = "https://zenodo.org/api/records/12/draft"
        key = "demo_1.0_20260101.simg"
        self.assertEqual([(c.args[0]) for c in session.post.call_args_list], [
            "https://zenodo.org/api/records", f"{draft}/files",
            f"{draft}/files/{key}/commit", f"{draft}/actions/publish",
        ])
        self.assertEqual(session.put.call_args.args[0], f"{draft}/files/{key}/content")
        self.assertEqual(session.put.call_args.kwargs["headers"]["Content-Length"], "3")
        self.assertTrue(all(c.kwargs["headers"]["Accept"] == RDM_JSON
                            for c in session.post.call_args_list))
        body = session.post.call_args_list[0].kwargs["json"]
        metadata = body["metadata"]
        self.assertEqual(metadata["rights"], LICENSE_INFO["rights"])
        self.assertEqual(metadata["resource_type"], {"id": "software"})
        self.assertEqual(metadata["publisher"], "Zenodo")
        self.assertEqual(metadata["creators"][0]["person_or_org"]["name"], "Neurodesk")
        self.assertIn("github.com/Example/microcontainers/tree/" + "a" * 40, metadata["description"])
        self.assertEqual(body["files"], {"enabled": True})
        session.delete.assert_not_called()

    def test_failed_publication_discards_the_draft(self) -> None:
        cases = {
            "upload": {"upload": reply({}, status=503)},
            "size": {"commit": reply({"size": 2})},
            "publish": {"publish": reply({}, status=400)},
            "field errors": {"create": reply({"id": "12", "errors": [
                {"field": "metadata.publisher", "messages": ["Missing"]}]})},
        }
        for label, overrides in cases.items():
            session = records_session(**overrides)
            with self.assertRaises((requests.HTTPError, ValueError), msg=label):
                self.publish(session)
            session.delete.assert_called_once()
            self.assertEqual(session.delete.call_args.args[0],
                             "https://zenodo.org/api/records/12/draft", label)

    def test_missing_files_error_on_new_draft_is_expected(self) -> None:
        session = records_session(created_errors=[{"field": "files.enabled",
                                                   "messages": ["Missing uploaded files."]}])
        self.assertEqual(self.publish(session), "10.5281/zenodo.12")

    def test_user_records_keeps_published_exact_titles_newest_first(self) -> None:
        session = MagicMock()
        session.get.return_value = reply({"hits": {"hits": [
            {"is_published": True, "updated": "2026-01-01", "metadata": {"title": "demo_1.0_20260101"},
             "pids": {"doi": {"identifier": "10.123/old"}}},
            {"is_published": True, "updated": "2026-02-01", "metadata": {"title": "demo_1.0_20260101"},
             "pids": {"doi": {"identifier": "10.123/new"}}},
            {"is_published": False, "metadata": {"title": "demo_1.0_20260101"}},
            {"is_published": True, "metadata": {"title": "demo_1.0_20260101-extra"}},
        ]}})
        rows = user_records(session, {"demo_1.0_20260101"})
        self.assertEqual(set(rows), {"demo_1.0_20260101"})
        self.assertEqual(record_doi(rows["demo_1.0_20260101"]), "10.123/new")
        call = session.get.call_args
        self.assertEqual(call.args[0], "https://zenodo.org/api/user/records")
        self.assertEqual(call.kwargs["headers"], {"Accept": RDM_JSON})
        self.assertEqual(call.kwargs["params"]["q"],
                         'is_published:true AND (metadata.title:"demo_1.0_20260101")')
        self.assertEqual(user_records(session, set()), {})

    def test_user_records_pages_and_chunks_title_queries(self) -> None:
        session = MagicMock()
        full = {"hits": {"hits": [{"is_published": True, "metadata": {"title": f"t_1_{i:08d}"}}
                                  for i in range(100)]}}
        session.get.side_effect = [reply(full), reply({"hits": {"hits": []}})]
        self.assertEqual(len(user_records(session)), 100)
        self.assertEqual([c.kwargs["params"]["page"] for c in session.get.call_args_list], [1, 2])
        session = MagicMock()
        session.get.return_value = reply({"hits": {"hits": []}})
        user_records(session, {f"t_1_{i:08d}" for i in range(120)})
        self.assertEqual(session.get.call_count, 3)

if __name__ == "__main__":
    unittest.main()
