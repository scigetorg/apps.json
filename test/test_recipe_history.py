"""Licenses come from the newest recipe commit declaring the image's version."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import catalog_licenses, zenodo_catalog
from scripts.doi_license import LicenseValidationError, rights_from_catalog, same_rights
from scripts.license_terms import render_copyright
from scripts.recipe_history import RecipeHistory, SourceRecipes, source_roots

REPO = "Example/containers"


def recipe(version, *licenses, name="demo"):
    lines = [f"name: {name}", f"version: {version}", "copyright:"]
    lines += [f"  - license: {value}" for value in licenses]
    return "\n".join(lines) + "\n"


class SourceClone:
    """A throwaway source repository with dated commits."""

    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.org")
        self.git("config", "user.name", "Test")
        self.day = 0

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def commit(self, files=None, delete=()):
        for path, text in (files or {}).items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for path in delete:
            self.git("rm", "-q", path)
        self.git("add", "-A")
        self.day += 1
        date = f"2026-01-{self.day:02d}T00:00:00Z"
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", f"day {self.day}"],
                       check=True, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                                        "PATH": "/usr/bin:/bin"})
        return self.git("rev-parse", "HEAD").strip()


def release(version, build_date, image=None, **extra):
    app = {"version": build_date, **({"image": image} if image else {})}
    return json.dumps({"apps": {f"demo {version}": app}, "categories": ["x"], **extra})


class RecipeHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clone = SourceClone(Path(directory.name))

    def test_newest_commit_declaring_the_version_wins(self):
        self.clone.commit({"recipes/demo/build.yaml": recipe("1.10", "MIT")})
        fixed = self.clone.commit({"recipes/demo/build.yaml": recipe("1.10", "MIT", "Apache-2.0")})
        self.clone.commit({"recipes/demo/build.yaml": recipe("2.0", "GPL-3.0-only"),
                           "releases/demo/1.10.json": release("1.10", "20260102")})
        history = RecipeHistory(self.clone.root, REPO)
        info = history.license("demo_1.10_20260102")
        # A license fixed without a version bump applies; 1.10 is not read as 1.1.
        self.assertEqual(info["revision"], fixed)
        self.assertEqual(info["declared"], ["Apache-2.0", "MIT"])
        self.assertEqual((info["recipe"], info["version"], info["source_repository"]),
                         ("demo", "1.10", REPO))
        with self.assertRaisesRegex(LicenseValidationError, "declares version '1.1'"):
            history.at_version("demo", "1.1")

    def test_invalid_yaml_commits_are_skipped(self):
        good = self.clone.commit({"recipes/demo/build.yaml": recipe("1.0", "MIT")})
        self.clone.commit({"recipes/demo/build.yaml": "name: demo\nversion: 1.0\n - broken\n"})
        self.assertEqual(RecipeHistory(self.clone.root, REPO).at_version("demo", "1.0")[0], good)

    def test_retired_recipe_and_arm64_release_resolve_from_history(self):
        self.clone.commit({"recipes/demo/build.yaml": recipe("1.0", "MIT"),
                           "releases/demo_arm64/1.0.json": release("1.0", "20260101",
                                                                   image="demo_1.0_arm64")})
        self.clone.commit(delete=["recipes/demo/build.yaml"])
        history = RecipeHistory(self.clone.root, REPO)
        self.assertEqual(history.index()["demo_1.0_arm64_20260101"], ("demo", "1.0"))
        self.assertEqual(history.license("demo_1.0_arm64_20260101")["declared"], ["MIT"])
        # Some arm64 releases are named "<version>-arm64"; the recipe declares "<version>".
        self.assertEqual(history.at_version("demo", "1.0-arm64")[1]["version"], "1.0")

    def test_recipe_name_from_release_json(self):
        self.clone.commit({"recipes/viewer/build.yaml": recipe("3.0", "MIT", name="viewer"),
                           "releases/demo/3.0.json": release("3.0", "20260101", recipe="viewer")})
        self.assertEqual(RecipeHistory(self.clone.root, REPO).index()["demo_3.0_20260101"],
                         ("viewer", "3.0"))

    def test_missing_version_or_release_is_reported(self):
        self.clone.commit({"recipes/demo/build.yaml": recipe("2.0", "MIT"),
                           "releases/demo/1.0.json": release("1.0", "20200101")})
        recipes = SourceRecipes({REPO: self.clone.root})
        with self.assertRaisesRegex(LicenseValidationError, "declares version '1.0'"):
            recipes.license("demo_1.0_20200101", [REPO])
        with self.assertRaisesRegex(LicenseValidationError, "no release JSON"):
            recipes.license("other_1.0_20200101", [REPO])
        # The publish plan skips such images instead of failing them every night.
        reason = zenodo_catalog.recipe_reason(recipes, "demo_1.0_20200101",
                                              {"source_repositories": [REPO]})
        self.assertIn("declares version '1.0'", reason)

    def test_templated_license_urls_are_rendered_for_catalog_and_publication(self):
        text = "\n".join([
            "name: demo", "version: 1.1.0",
            "variables:", "  upstream_version: '{{ context.version }}-rc'",
            "  arch_url:", "    x86_64: a", "    aarch64: b",
            "copyright:",
            "  - name: Demo terms",
            "    url: https://example.org/demo/blob/v{{ context.version }}/LICENSE.md",
            "  - license: MIT",
            "    url: https://example.org/{{context.name}}/{{ upstream_version }}/LICENSE",
        ]) + "\n"
        self.clone.commit({"recipes/demo/build.yaml": text,
                           "releases/demo/1.1.0.json": release("1.1.0", "20260101")})
        recipes = SourceRecipes({REPO: self.clone.root})
        title = "demo_1.1.0_20260101"
        catalogue = {"mit": {"id": "mit", "title": "MIT", "url": None}}
        info = recipes.license(title, [REPO], catalogue)
        terms = "https://example.org/demo/blob/v1.1.0/LICENSE.md"
        self.assertEqual(info["license_details"], [{"name": "Demo terms", "url": terms}])
        self.assertEqual(info["rights"], [{"title": {"en": "Demo terms"}, "link": terms},
                                          {"id": "mit"}])
        document = recipes.history(REPO).at_version("demo", "1.1.0")[1]
        # Nested variables and the no-space form render too.
        self.assertEqual(render_copyright(document)["copyright"][1]["url"],
                         "https://example.org/demo/1.1.0-rc/LICENSE")
        # The cached document is untouched.
        self.assertIn("{{ context.version }}", document["copyright"][0]["url"])
        # The catalog entry built from it agrees with the recipe at publish time.
        entry = catalog_licenses.resolve_artifacts(
            {title: {"repository": REPO, "root": str(self.clone.root)}})["resolved"][title]
        self.assertTrue(same_rights(info["rights"], rights_from_catalog(entry)))

    def test_unrenderable_license_template_is_reported(self):
        for url in ("https://example.org/{{ context.version | replace('.', '') }}/LICENSE",
                    "https://example.org/{{ context.missing }}/LICENSE",
                    "https://example.org/{{ context.arch_url }}/LICENSE",
                    "https://example.org/{% if true %}x{% endif %}/LICENSE"):
            text = "\n".join(["name: demo", "version: '1.0'", "variables:",
                              "  arch_url:", "    x86_64: a",
                              "copyright:", "  - name: Demo terms", f"    url: \"{url}\""]) + "\n"
            self.clone.commit({"recipes/demo/build.yaml": text,
                               "releases/demo/1.0.json": release("1.0", "20260101")})
            recipes = SourceRecipes({REPO: self.clone.root})
            with self.assertRaisesRegex(LicenseValidationError, "cannot render", msg=url):
                recipes.license("demo_1.0_20260101", [REPO])
            # The plan skips the image with the reason instead of failing at publish.
            self.assertIn("cannot render", zenodo_catalog.recipe_reason(
                recipes, "demo_1.0_20260101", {"source_repositories": [REPO]}), url)

    def test_source_roots_match_workflow_checkout_layout(self):
        # scripts/workflow.mjs sourcePath: join(root, ".catalog-sources", encodeURIComponent(repo))
        self.assertEqual(source_roots(Path("/x/.catalog-sources"), ["NeuroDesk/neurocontainers"]),
                         {"NeuroDesk/neurocontainers": Path("/x/.catalog-sources/NeuroDesk%2Fneurocontainers")})

    def test_shallow_clone_is_refused(self):
        self.clone.commit({"recipes/demo/build.yaml": recipe("1.0", "MIT")})
        self.clone.commit({"recipes/demo/build.yaml": recipe("1.0", "Apache-2.0")})
        with tempfile.TemporaryDirectory() as directory:
            shallow = Path(directory) / "shallow"
            subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{self.clone.root}",
                            str(shallow)], check=True)
            with self.assertRaisesRegex(RuntimeError, "shallow clone"):
                RecipeHistory(shallow, REPO)

    def test_consolidate_resolver_reports_errors_per_image(self):
        self.clone.commit({"recipes/demo/build.yaml": recipe("1.0", "other-closed"),
                           "releases/demo/1.0.json": release("1.0", "20260101"),
                           "releases/demo/0.9.json": release("0.9", "20250101")})
        source = {"repository": REPO, "root": str(self.clone.root)}
        result = catalog_licenses.resolve_artifacts(
            {"demo_1.0_20260101": source, "demo_0.9_20250101": source})
        self.assertEqual(result["resolved"], {"demo_1.0_20260101": {
            "license": ["other-closed"], "license_details": [{"license": "other-closed"}]}})
        self.assertIn("declares version '0.9'", result["errors"]["demo_0.9_20250101"])


if __name__ == "__main__":
    unittest.main()
