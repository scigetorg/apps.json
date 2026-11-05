"""Resolve catalog licenses from the recipe recorded by each built image."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .doi_metadata import resolve_build_license


def resolve_one(title, github_token, source_repositories=None):
    info = resolve_build_license(title, github_token, source_repositories)
    return {"license": info["declared"], "license_details": info["license_details"]}


def resolve_artifacts(titles, github_token, workers=8, source_repositories_by_title=None):
    resolved = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_one, title, github_token,
                               (source_repositories_by_title or {}).get(title)): title
                   for title in titles}
        for future in as_completed(futures):
            title = futures[future]
            try:
                resolved[title] = future.result()
            except (OSError, RuntimeError, ValueError) as exc:
                errors[title] = str(exc)
    return {"resolved": resolved, "errors": errors}


def main():
    payload = json.loads(sys.stdin.read())
    titles = payload.get("titles")
    if not isinstance(titles, list) or any(not isinstance(title, str) for title in titles):
        raise ValueError("titles must be a list of image names")
    sources = payload.get("source_repositories_by_title") or {}
    if not isinstance(sources, dict) or any(
        not isinstance(repositories, list) or not repositories or
        any(not isinstance(repository, str) for repository in repositories)
        for repositories in sources.values()
    ):
        raise ValueError("source_repositories_by_title must map images to repository arrays")
    print(json.dumps(resolve_artifacts(
        titles, os.environ.get("GITHUB_TOKEN"), source_repositories_by_title=sources
    )))


if __name__ == "__main__":
    main()
