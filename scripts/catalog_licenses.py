"""Resolve catalog licenses from the recipe recorded by each built image."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .doi_metadata import get_build_recipe
from .license_terms import declared_licenses, license_details


def resolve_one(title, github_token):
    recipe, _ = get_build_recipe(title, github_token)
    return {"license": declared_licenses(recipe),
            "license_details": license_details(recipe)}


def resolve_artifacts(titles, github_token, workers=8):
    resolved = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_one, title, github_token): title
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
    print(json.dumps(resolve_artifacts(titles, os.environ.get("GITHUB_TOKEN"))))


if __name__ == "__main__":
    main()
