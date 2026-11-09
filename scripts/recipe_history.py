"""Licenses from recipe git history: the newest commit declaring an image's version.

An image `<image>_<build date>` comes from `releases/<tool>/<release>.json`, whose
file name is the recipe version. Its license is the `copyright` of the newest
commit of `recipes/<recipe>/build.yaml` on the source clone whose `version:`
equals that release. The newest commit is used on purpose: license fixes made
without a version bump (and so without a rebuild) apply to the existing build.

    release JSON ─► (recipe, version) ─► git log recipes/<recipe>/build.yaml
        ─► newest commit with `version: <release>` ─► copyright ─► license

No registry or network calls: the source clone needs full history
(`node scripts/workflow.mjs checkout`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from .doi_license import LicenseValidationError, resolve_recipe_license
from .license_terms import declared_licenses, license_details, render_copyright


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}")
    return result.stdout


class RecipeHistory:
    """Recipe commits of one source clone, read once and cached."""

    def __init__(self, root: Path | str, repository: str):
        self.root = Path(root)
        self.repository = repository
        if _git(self.root, "rev-parse", "--is-shallow-repository").strip() == "true":
            raise RuntimeError(
                f"{self.root} is a shallow clone; recipe history needs the full history "
                "(remove it and run `node scripts/workflow.mjs checkout`)")
        self._commits: dict[str, list[str]] | None = None
        self._documents: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._index: dict[str, tuple[str, str]] | None = None

    def commits(self, recipe: str) -> list[str]:
        """Commits touching recipes/<recipe>/build.yaml, newest first (one git log)."""
        if self._commits is None:
            self._commits = {}
            commit = None
            for line in _git(self.root, "log", "--format=@%H", "--name-only", "HEAD",
                             "--", "recipes/*/build.yaml").splitlines():
                if line.startswith("@"):
                    commit = line[1:]
                elif line.startswith("recipes/") and line.endswith("/build.yaml") and commit:
                    self._commits.setdefault(line.split("/")[1], []).append(commit)
        return self._commits.get(recipe, [])

    def documents(self, recipe: str) -> list[tuple[str, dict[str, Any]]]:
        """(commit, recipe) pairs, newest first; deleted or invalid YAML is skipped.

        BaseLoader keeps every scalar a string, so `version: 1.10` stays "1.10".
        """
        if recipe not in self._documents:
            commits = self.commits(recipe)
            path = f"recipes/{recipe}/build.yaml"
            batch = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "--batch"],
                input="".join(f"{commit}:{path}\n" for commit in commits).encode(),
                capture_output=True, check=True,
            ).stdout
            documents, position = [], 0
            for commit in commits:
                end = batch.index(b"\n", position)
                header = batch[position:end].split()
                position = end + 1
                if len(header) < 3 or header[1] != b"blob":
                    continue
                size = int(header[2])
                text = batch[position:position + size].decode("utf-8", "replace")
                position += size + 1
                try:
                    document = yaml.load(text, Loader=yaml.BaseLoader)
                except yaml.YAMLError:
                    continue
                if isinstance(document, dict):
                    documents.append((commit, document))
            self._documents[recipe] = documents
        return self._documents[recipe]

    def at_version(self, recipe: str, version: str) -> tuple[str, dict[str, Any]]:
        # arm64 releases may be named "<version>-arm64" while the recipe says "<version>".
        wanted = [version] + [version[:-len(suffix)] for suffix in ("-arm64", "_arm64")
                              if version.endswith(suffix)]
        for candidate in wanted:
            for commit, document in self.documents(recipe):
                if str(document.get("version", "")).strip() == candidate:
                    return commit, document
        raise LicenseValidationError(
            f"No commit of recipes/{recipe}/build.yaml declares version {version!r}")

    def index(self) -> dict[str, tuple[str, str]]:
        """Image title -> (recipe, version) for every release JSON at HEAD."""
        if self._index is None:
            self._index = {}
            for path in sorted((self.root / "releases").glob("*/*.json")):
                tool, release = path.parent.name, path.stem
                data = json.loads(path.read_text(encoding="utf-8"))
                recipe = data.get("recipe") or tool
                if recipe == tool and tool.endswith("_arm64") and not self.commits(recipe):
                    recipe = tool[:-len("_arm64")]
                for app in (data.get("apps") or {}).values():
                    image = app.get("image") or f"{tool}_{release}"
                    self._index[f"{image}_{app.get('version')}"] = (recipe, release)
        return self._index

    def license(self, title: str, catalogue: dict[str, Any] | None = None) -> dict[str, Any]:
        """License info for an image title; `rights` too when Zenodo's catalogue is given."""
        if title not in self.index():
            raise LicenseValidationError(f"{title} has no release JSON in {self.repository}")
        recipe, version = self.index()[title]
        commit, document = self.at_version(recipe, version)
        try:
            # Catalog, plan and publish all read licenses here, so they all see the
            # same rendered URLs (e.g. .../blob/v{{ context.version }}/LICENSE).
            document = render_copyright(document)
        except ValueError as exc:
            raise LicenseValidationError(
                f"recipes/{recipe}/build.yaml at {commit[:12]}: {exc}") from exc
        info = resolve_recipe_license(document, catalogue) if catalogue is not None else {}
        info.update(
            declared=declared_licenses(document) if document.get("copyright") else [],
            license_details=license_details(document),
            revision=commit, recipe=recipe, version=version,
            source_repository=self.repository,
        )
        return info


class SourceRecipes:
    """Recipe histories of the configured source clones, keyed by repository."""

    def __init__(self, roots: dict[str, Path | str]):
        self.roots = roots
        self._histories: dict[str, RecipeHistory] = {}

    def history(self, repository: str) -> RecipeHistory:
        if repository not in self._histories:
            if repository not in self.roots:
                raise LicenseValidationError(f"No source clone for {repository}")
            self._histories[repository] = RecipeHistory(self.roots[repository], repository)
        return self._histories[repository]

    def license(self, title: str, repositories: list[str],
                catalogue: dict[str, Any] | None = None) -> dict[str, Any]:
        for repository in repositories:
            history = self.history(repository)
            if title in history.index():
                return history.license(title, catalogue)
        raise LicenseValidationError(f"{title} has no release JSON in {', '.join(repositories)}")


def source_roots(sources_root: Path, repositories: list[str]) -> dict[str, Path]:
    """Clone paths as `node scripts/workflow.mjs checkout` lays them out (sourcePath):
    one directory per repository, named encodeURIComponent("owner/repo")."""
    return {repository: sources_root / quote(repository, safe="") for repository in repositories}
