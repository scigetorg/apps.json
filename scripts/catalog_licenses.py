"""Resolve catalog licenses from recipe history (see recipe_history.py).

Reads JSON on stdin: {"titles": {<image title>: {"repository": "owner/repo",
"root": "<source clone>"}}}; prints {"resolved": {title: {license,
license_details}}, "errors": {title: reason}}.
"""

import json
import sys

from .recipe_history import SourceRecipes


def resolve_artifacts(titles):
    recipes = SourceRecipes({source["repository"]: source["root"] for source in titles.values()})
    resolved, errors = {}, {}
    for title, source in titles.items():
        try:
            info = recipes.license(title, [source["repository"]])
            resolved[title] = {"license": info["declared"], "license_details": info["license_details"]}
        except (OSError, RuntimeError, ValueError) as exc:
            errors[title] = str(exc)
    return {"resolved": resolved, "errors": errors}


def main():
    titles = json.loads(sys.stdin.read()).get("titles")
    if not isinstance(titles, dict) or any(
        not isinstance(source, dict) or not isinstance(source.get("repository"), str)
        or not isinstance(source.get("root"), str) for source in titles.values()
    ):
        raise ValueError("titles must map image names to {repository, root}")
    print(json.dumps(resolve_artifacts(titles)))


if __name__ == "__main__":
    main()
