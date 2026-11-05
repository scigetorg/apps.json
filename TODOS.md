# TODOs

## Retire neurocontainers' legacy apps.json pipeline

- **What:** Delete `neurocontainers/.github/workflows/update-apps-json.yml`, and
  `tools/generate_apps_json.py` if nothing else uses it.
- **Why:** It is a second catalog generator. On every release push it rebuilds
  `apps.json` and force-pushes it to `neurodesk/neurocommand`
  (`neurodesk/apps.json`), so it can drift from this repo's consolidate output.
- **Pros:** One catalog generator, no extra push or token use per release.
- **Cons:** Anything still reading `neurocommand/neurodesk/apps.json` breaks.
- **Context:** This repo replaced that pipeline (consolidate → Cloudflare Worker).
  Start by finding neurocommand's consumers of `neurodesk/apps.json`.
- **Depends on:** Confirming neurocommand reads the catalog from this repo or
  its Cloudflare Worker.

## Clean up `zenodo_license` and custom-license names in neurocontainers

- **What:** Remove `zenodo_license` from `recipes/{dicomtools,metabody,bidstools,cryosparc}/build.yaml`
  and from the `builder/validation.py` messages that suggest `other-open`/`other-closed`. Add a
  `name` to `LicenseRef-…` copyright entries that lack one (stir and startrack on `update-licenses`;
  also mede, quantiphyse, topofit and vmtk on `origin/main`). The images already built from those
  recipes need a rebuild after the fix, because DOIs use the recipe at the image's build revision.
- **Why:** DOIs now go through Zenodo's records API, where each license is its own `rights`
  entry and custom terms publish by name + URL. `zenodo_license` is ignored, and custom entries
  without a name cannot be published.
- **Depends on:** the records-API publication change in this repo being on `main`.

## Decide whether `other-at` records count as legacy

- **What:** Records published with the old `other-at` ("Other (Attribution)") ID are reported as
  `mismatch`, not `verified_legacy`, so the backfill skips them.
- **Why:** Seen on sandbox (4 records). If they are legacy single-license records, add `other-at`
  to the audit's legacy rule and the backfill will fix them too.
