"""The backfill edits only legacy records, and only their rights."""

import unittest
from unittest.mock import MagicMock, patch

import requests

from scripts import backfill_zenodo_rights as backfill
from scripts.zenodo_catalog import ZENODO_API

TITLE = "demo_1.0_20260101"
DETAILS = [{"name": "Research terms", "url": "https://example.org/terms"}]
ARTIFACTS = {
    TITLE: {"license": ["MIT", "Research terms"], "license_details": DETAILS},
    "single_1.0_20260101": {"license": ["MIT"]},
}
EXPECTED = [{"id": "mit"}, {"title": {"en": "Research terms"}, "link": "https://example.org/terms"}]


def record(rights, title=TITLE, record_id="12"):
    return {"id": record_id, "is_published": True,
            "pids": {"doi": {"identifier": f"10.5281/zenodo.{record_id}"}},
            "metadata": {"title": title, "rights": rights, "resource_type": {"id": "software"},
                         "publisher": "Zenodo"},
            "files": {"enabled": True}, "access": {"record": "public", "files": "public"}}


RECORDS = {
    TITLE: record([{"id": "other-open"}]),
    "single_1.0_20260101": record([{"id": "mit"}], title="single_1.0_20260101", record_id="13"),
}


def reply(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.side_effect = (
        requests.HTTPError(str(status)) if status >= 400 else None)
    return response


def edit_session(update=None, publish=None):
    session = MagicMock()
    session.post.side_effect = [
        reply({**record([{"id": "other-open", "title": {"en": "Other (Open)"}}]), "is_draft": True}),
        publish or reply(record(EXPECTED)),
    ]
    session.put.return_value = update or reply({"errors": []})
    return session


ZENODO_IDS = {"mit": {}, "gpl-2.0-only": {}, "lgpl-2.1-only": {}}


def run(*argv, session=None, artifacts=ARTIFACTS, records=RECORDS):
    session = session or edit_session()
    with (patch("sys.argv", ["backfill", *argv]),
          patch.dict("os.environ", {"ZENODO_TOKEN": "token"}, clear=True),
          patch.object(backfill, "build_session", return_value=session),
          patch.object(backfill, "catalog_artifacts", return_value=artifacts),
          patch.object(backfill, "user_records", return_value=records),
          patch.object(backfill, "fetch_catalogue", return_value={"entries": ZENODO_IDS})):
        return backfill.main(), session


class BackfillTests(unittest.TestCase):
    def test_targets_only_verified_legacy_records(self):
        rows = backfill.legacy_targets(ARTIFACTS, RECORDS)
        self.assertEqual([row["title"] for row in rows], [TITLE])
        self.assertEqual(backfill.legacy_targets(ARTIFACTS, RECORDS, {"other"}), [])

    def test_dry_run_writes_nothing(self):
        code, session = run()
        self.assertEqual(code, 0)
        session.post.assert_not_called()
        session.put.assert_not_called()
        session.delete.assert_not_called()

    def test_apply_replaces_only_rights_and_keeps_the_doi(self):
        code, session = run("--apply", "--limit", "1")
        self.assertEqual(code, 0)
        draft_url = f"{ZENODO_API}/records/12/draft"
        self.assertEqual([call.args[0] for call in session.post.call_args_list],
                         [draft_url, f"{draft_url}/actions/publish"])
        sent = session.put.call_args.kwargs["json"]
        self.assertEqual(sent["metadata"]["rights"], EXPECTED)
        self.assertEqual(sent["metadata"]["publisher"], "Zenodo")
        self.assertEqual(sent["access"], {"record": "public", "files": "public"})
        self.assertNotIn("is_draft", sent)
        session.delete.assert_not_called()

    def test_noassertion_record_with_zenodo_default_is_cleared(self):
        title = "tool_1.0_20260101"
        artifacts = {title: {"license": ["NOASSERTION"], "license_details": [
            {"license": "NOASSERTION", "url": "https://example.org/src"}]}}
        records = {title: record([{"id": "cc-by-4.0"}], title=title)}
        code, session = run("--apply", session=edit_session(publish=reply(record([], title=title))),
                            artifacts=artifacts, records=records)
        self.assertEqual(code, 0)
        self.assertEqual(session.put.call_args.kwargs["json"]["metadata"]["rights"], [])

    def test_rejected_update_discards_the_edit_draft(self):
        code, session = run("--apply", session=edit_session(
            update=reply({"errors": [{"field": "metadata.rights", "messages": ["bad"]}]})))
        self.assertEqual(code, 1)
        session.delete.assert_called_once()
        self.assertEqual(session.delete.call_args.args[0], f"{ZENODO_API}/records/12/draft")

    def test_deprecated_spdx_ids_are_written_as_zenodo_ids(self):
        # civet: apps.json lists GPL-2.0 and LGPL-2.1; Zenodo only knows -only/-or-later
        # and answers "400 Invalid value gpl-2.0." to the old spelling.
        artifacts = {TITLE: {"license": ["GPL-2.0", "LGPL-2.1", "Research terms"],
                             "license_details": DETAILS}}
        expected = [{"id": "gpl-2.0-only"}, {"id": "lgpl-2.1-only"},
                    {"title": {"en": "Research terms"}, "link": "https://example.org/terms"}]
        code, session = run("--apply", artifacts=artifacts, session=edit_session(
            publish=reply(record(expected))))
        self.assertEqual(code, 0)
        self.assertEqual(session.put.call_args.kwargs["json"]["metadata"]["rights"], expected)

    def test_unknown_license_id_fails_before_any_write(self):
        artifacts = {TITLE: {"license": ["Odd-1.0", "Research terms"], "license_details": DETAILS}}
        for argv in ((), ("--apply",)):
            with patch("sys.stderr") as stderr:
                code, session = run(*argv, artifacts=artifacts)
            self.assertEqual(code, 1, argv)
            session.post.assert_not_called()
            self.assertIn("not Zenodo license IDs: ['odd-1.0']",
                          "".join(str(c.args[0]) for c in stderr.write.call_args_list))

    def test_zenodo_error_message_is_reported(self):
        rejected = reply({"status": 400, "message": "Invalid value gpl-2.0."}, status=400)
        with self.assertRaisesRegex(Exception, "HTTP 400 from Zenodo: Invalid value gpl-2.0."):
            backfill.raise_for_zenodo(rejected)

    def test_changed_doi_after_publish_is_a_failure(self):
        changed = record(EXPECTED)
        changed["pids"]["doi"]["identifier"] = "10.5281/zenodo.99"
        code, _ = run("--apply", session=edit_session(publish=reply(changed)))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
