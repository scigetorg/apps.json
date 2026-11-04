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
