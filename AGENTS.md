# AGENTS.md

Context for coding agents working in this repository. `README.md` is the
operator-facing guide; this file covers what you need to change code safely.

## What this repo is

The Neurodesk apps catalog (`Neurodesk/apps.json`), migrated from
`neurocommand`. Scheduled GitHub Actions read release metadata from configured
containers repositories (currently `NeuroDesk/neurocontainers`), generate a
per-domain catalog, publish it to a Cloudflare Worker, mint Zenodo DOIs for
container images, and commit the results back to `main`.

## Layout

```text
domains.json                 # allowed domains -> source_repositories (owner/repo array)
<domain>/apps.json           # GENERATED catalog (neuroimaging is the only domain today)
<domain>/manifest.json       # GENERATED image count and source commits
scripts/workflow.mjs         # CLI: checkout | run | output-paths; multi-domain orchestration
scripts/catalog.mjs          # reads releases, builds catalog/manifest, license + DOI carry-over
scripts/cloudflare.mjs       # Worker static-asset upload + post-deploy URL verification
scripts/zenodo_catalog.py    # DOI CLI: plan | publish
scripts/doi_license.py       # recipe license resolution against Zenodo's live license catalog
scripts/recipe_history.py    # license = newest recipe commit declaring the image's version (local git)
scripts/doi_metadata.py      # records-API draft metadata
scripts/audit_doi_licenses.py# weekly read-only audit: Zenodo license vs apps.json -> JSON report
scripts/backfill_zenodo_rights.py # manual: rewrite verified_legacy records' rights from apps.json
test/*.test.mjs              # node:test suites (catalog, workflow, cloudflare)
test/test_*.py               # unittest suites (zenodo_catalog, doi_licenses, recipe_history, backfill)
```

`.catalog-sources/<owner>/<repo>/` holds local clones of source repositories
(gitignored), created by `node scripts/workflow.mjs checkout`.

## Generated files — do not hand-edit

`*/apps.json` and `*/manifest.json` are rewritten by CI. Most
commits on `main` are bot commits ("Update domain apps catalogs from container
releases", "Add published DOI metadata ..."). Fix data problems in the source
repository's recipes / release JSON, or fix the generator here. When a
code change alters output, regenerate locally rather than editing JSON by hand,
and keep unrelated regenerated diffs out of code PRs.

## Key rules encoded in the code

- Domain IDs: lowercase slug, letters/digits/single hyphens, starts with a
  letter. Stable even if the source repo is renamed.
- A source recipe declares `domains: [...]` in `recipes/<recipe>/build.yaml`;
  retired recipes declare them in `workflows/retired_recipes.yaml` in the
  source repo. Every release must resolve to a declared domain, and the source
  must be listed under that domain in `domains.json`.
- `source_repositories` must be an array (the old single `source-repository`
  command is retired and throws).
- Duplicate app or image identities across repositories fail generation.
- Each app carries the exact SIF `image` name (GUI launchers included).
  `show_in_menu` / `show_in_applist` appear only if the release JSON sets them.
  The catalog omits a `domains` field (the path implies it).
- Licenses come from recipe history (`scripts/recipe_history.py`): the newest
  commit of `recipes/<recipe>/build.yaml` whose `version:` equals the release
  file name. Consolidate resolves every image on every run, and publish
  resolves the same way; both need full-history source clones (`checkout`
  never uses `--depth`). Versions are read with `yaml.BaseLoader`, so `1.10`
  stays a string, and commits with invalid YAML are skipped. The stored
  `apps.json` license is kept only when history has no license for the
  version. Custom terms (`license_terms.is_custom_term`) are listed in
  `license` by their `name` (`license_terms.license_label`) and keep their name and
  URL in `license_details`; `rights_from_catalog` accepts the name or the older
  `other-*`/`LicenseRef-` form, and `rights_from_catalog` refuses to turn such
  terms into IDs. No registry (GHCR/Docker Hub) or OCI label is used.
- Never borrow a license from another version of a tool: licenses change
  between versions. An image with no recipe license for its version and none
  stored stays unlicensed (logged by the catalog run).
- Retiring an image = deleting its release JSON upstream; there is no
  `retired-images.json` any more. Deleting the last release produces an empty
  catalog. Cleanup is time-based: neurocontainers' `cleanup-stale-containers`
  (11:25 UTC) deletes SIFs absent from apps.json, releases and CVMFS once
  they are older than 7 days.
- DOI publication validates the license from recipe history and fails if
  `apps.json` licenses disagree; the plan skips images with no recipe license
  for their version. The weekly audit
  compares each Zenodo record with apps.json only (no recipe calls),
  using `doi_license.same_rights`. A Zenodo failure must not block catalog updates.
- Zenodo is used through its records API (InvenioRDM). Always send
  `Accept: application/vnd.inveniordm.v1+json` (otherwise `/api/user/records`
  answers in the legacy format with one license and no `pids`), write `rights`
  in bare form (`{id}` or `{title, link}`; Zenodo rejects its own expanded
  entries), and treat a non-empty `errors` list on a draft write as a failure
  even though the HTTP status is 200/201.

## Workflows (`.github/workflows/`)

All catalog-writing workflows share the `apps-catalog` concurrency group.

| Workflow | Trigger | Does |
| --- | --- | --- |
| `consolidate.yml` | daily 02:25 UTC, manual | checkout sources → `npm run consolidate` → commit outputs → Cloudflare publish |
| `publish-dois.yml` | daily 04:25 UTC, manual | Python tests → plan every missing licensed image (others skipped and listed; `limit` input for a smoke test) into ≤6 batches (`PUBLISH_JOBS`) → matrix publish, 6 in parallel, no new upload after 5 h (skipped when nothing is missing) |
| `audit-doi-licenses.yml` | Sundays 05:00 UTC, manual | read-only audit, uploads JSON artifact |
| `backfill-zenodo-rights.yml` | manual only | dry run by default; `apply` edits `verified_legacy` records' license list (same DOI) |
| `test.yml` | PRs and code pushes to `main` | `npm test` + Python unittest, no secrets |

Never add `pull_request` triggers to workflows that receive `ZENODO_TOKEN`:
`publish-dois` mints permanent production DOIs.

Secrets/vars: `ZENODO_TOKEN` (production zenodo.org, deposit:write +
deposit:actions), `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, optional
`CLOUDFLARE_WORKER_NAME` (default `apps-json`), `GITHUB_TOKEN`.

## Commands

Node.js 24 (ESM, only dependency is `yaml`); Python 3.11 with `requests` and
`pyyaml>=6.0.2`.

```bash
npm ci
npm test                                            # all node:test suites
python -m unittest discover -s test -p 'test_*.py'  # Python DOI tests

node scripts/workflow.mjs checkout                  # clone sources (network)
npm run consolidate                                 # regenerate every domain
node scripts/workflow.mjs output-paths              # files CI commits
python -m scripts.zenodo_catalog plan --catalog-root .
```

Run Python scripts as modules (`python -m scripts.<name>`) from the repo root.

## Things that touch production — ask first

- `npm run publish:cloudflare` updates the public Worker.
- `python -m scripts.zenodo_catalog publish` mints permanent production DOIs.
- `python -m scripts.backfill_zenodo_rights --apply` edits published production records.

Tests must not hit the network; existing tests stub `fetch`/`requests`.
Follow that pattern when adding tests.
