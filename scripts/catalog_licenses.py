"""Resolve catalog licenses from the recipe recorded by each built image."""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .doi_metadata import get_build_recipe
from .license_terms import declared_licenses, license_details


def enrich_record(record):
    recipe = {"copyright": record["copyright"]}
    return {**record, "license": declared_licenses(recipe),
            "license_details": license_details(recipe)}


def resolve_one(title, github_token):
    recipe, provenance = get_build_recipe(title, github_token)
    return enrich_record({
        "revision": provenance["revision"],
        "recipe": provenance["recipe"],
        "copyright": recipe.get("copyright") or [],
        "zenodo_license": recipe.get("zenodo_license"),
    })


def resolve_artifacts(titles, github_token, cache=None, workers=8):
    cache = cache or {}
    resolved = {}
    errors = {}
    for title in titles:
        if title in cache:
            try:
                resolved[title] = enrich_record(cache[title])
            except (KeyError, TypeError, ValueError) as exc:
                errors[title] = str(exc)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_one, title, github_token): title
                   for title in titles if title not in cache}
        for future in as_completed(futures):
            title = futures[future]
            try:
                resolved[title] = future.result()
            except (OSError, RuntimeError, ValueError) as exc:
                errors[title] = str(exc)
    return {"resolved": resolved, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-json")
    args = parser.parse_args()
    payload = json.loads(sys.stdin.read()) if args.artifacts_json is None else {
        "titles": json.loads(args.artifacts_json), "cache": {}}
    titles = payload.get("titles")
    cache = payload.get("cache", {})
    if not isinstance(titles, list) or any(not isinstance(title, str) for title in titles):
        parser.error("titles must be a list of image names")
    if not isinstance(cache, dict):
        parser.error("cache must be an object")
    print(json.dumps(resolve_artifacts(titles, os.environ.get("GITHUB_TOKEN"), cache)))


if __name__ == "__main__":
    main()
