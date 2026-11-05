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
  also quantiphyse, topofit and vmtk on `origin/main`).
- **Why:** DOIs now go through Zenodo's records API, where each license is its own `rights`
  entry and custom terms publish by name + URL. `zenodo_license` is ignored, and custom entries
  without a name cannot be published.
- **Depends on:** the records-API publication change in this repo being on `main`.

## Backfill multi-license `rights` on legacy Zenodo records

- **What:** Step 5 of the records-API plan: a manual, dry-run-by-default job that edits the audit's
  `verified_legacy` records so their `rights` list every license from apps.json (same DOI).
- **Why:** Records published through the legacy API carry one license each.
- **Context:** Verified on sandbox: edit draft → PUT bare rights → publish keeps the DOI, version
  and files. Decide whether legacy `other-at` records count as legacy too (seen on sandbox).
