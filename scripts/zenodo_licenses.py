"""Write the license IDs a recipe can use and still get a Zenodo DOI.

The list is the intersection of Zenodo's license catalogue (what a DOI record
accepts) and the SPDX table in Python `packaging` (what the NeuroContainers
builder accepts), in SPDX casing. Zenodo-only IDs such as `other-open` are left
out: the builder rejects them, so recipes declare those terms as custom
licenses with a name and URL. `deprecated_aliases` lists the old SPDX IDs that
publication maps onto current Zenodo IDs (`doi_license.DEPRECATED_SPDX`).

The file is published next to the catalogs on the Cloudflare Worker and read by
neurocontainers-ui. DOI publication itself still validates against Zenodo live.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import packaging
from packaging.licenses._spdx import LICENSES

from .doi_license import CATALOGUE_URL, DEPRECATED_SPDX, CatalogueError, fetch_catalogue

DEFAULT_OUTPUT = Path("licenses") / "zenodo.json"
# Zenodo lists ~450 licenses; far fewer means a truncated or broken response.
MINIMUM_LICENSES = 300


def build_license_list(entries, spdx=LICENSES, packaging_version=packaging.__version__):
    """Build the published document from Zenodo catalogue entries (keyed by lowercase ID)."""
    licenses = []
    for key, entry in entries.items():
        spdx_entry = spdx.get(key)
        if spdx_entry is None or spdx_entry["deprecated"]:
            continue
        licenses.append({
            "id": spdx_entry["id"],
            "title": entry["title"],
            "url": entry["url"],
            "osi_approved": entry["osi_approved"],
        })
    if len(licenses) < MINIMUM_LICENSES:
        raise CatalogueError(
            f"Only {len(licenses)} Zenodo licenses are SPDX IDs; expected at least {MINIMUM_LICENSES}"
        )
    licenses.sort(key=lambda row: row["id"].lower())

    listed = {row["id"].lower(): row["id"] for row in licenses}
    aliases = {}
    for old, new in DEPRECATED_SPDX.items():
        base = old.removesuffix("+")
        if new not in listed or base not in spdx:
            continue
        aliases[spdx[base]["id"] + old[len(base):]] = listed[new]
    return {
        "source": {"zenodo": CATALOGUE_URL, "packaging": packaging_version},
        "licenses": licenses,
        "deprecated_aliases": dict(sorted(aliases.items(), key=lambda item: item[0].lower())),
    }


def write_json(path, document):
    """Write atomically so a failed run never leaves a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=1, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = build_license_list(fetch_catalogue()["entries"], LICENSES)
    except CatalogueError as exc:
        print(f"Zenodo license list not updated: {exc}", file=sys.stderr)
        return 1
    write_json(args.output, document)
    print(f"Wrote {len(document['licenses'])} licenses and "
          f"{len(document['deprecated_aliases'])} aliases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
