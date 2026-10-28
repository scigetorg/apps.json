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
<domain>/logs.txt            # GENERATED CVMFS inventory: one image_version_build per line
<domain>/manifest.json       # GENERATED logs.txt sha256, entry count, source commit(s)
scripts/workflow.mjs         # CLI: checkout | run | output-paths; multi-domain orchestration
scripts/catalog.mjs          # reads releases, builds catalog/logs/manifest, license + DOI carry-over
scripts/cloudflare.mjs       # Worker static-asset upload + post-deploy URL verification
scripts/zenodo_catalog.py    # DOI CLI: plan | publish | enrich
scripts/doi_license.py       # recipe license resolution against Zenodo's live license catalog
scripts/doi_provenance.py    # reads OCI image build revision from the registry (GHCR, Docker Hub fallback)
scripts/doi_metadata.py      # builds Zenodo deposition metadata
scripts/audit_doi_licenses.py# weekly read-only license audit -> JSON report
test/*.test.mjs              # node:test suites (catalog, workflow, cloudflare)
test/test_*.py               # unittest suites (zenodo_catalog, doi_licenses)
```

`.catalog-sources/<owner>/<repo>/` holds local clones of source repositories
(gitignored), created by `node scripts/workflow.mjs checkout`.

## Generated files — do not hand-edit

`*/apps.json`, `*/logs.txt`, and `*/manifest.json` are rewritten by CI. Most
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
- Existing `doi` and `license` values follow an app across domain moves when
  release metadata can't supply them.
- Retiring an image = deleting its release JSON upstream; there is no
  `retired-images.json` any more.
- DOI publication validates the license against the recipe at the image's OCI
  build revision and fails if `apps.json` licenses disagree. A Zenodo failure
  must not block catalog updates.

## Workflows (`.github/workflows/`)

All catalog-writing workflows share the `apps-catalog` concurrency group.

| Workflow | Trigger | Does |
| --- | --- | --- |
| `consolidate.yml` | daily 02:25 UTC, manual | checkout sources → `npm run consolidate` → Cloudflare publish → commit outputs |
| `publish-catalog.yml` | push to `main` touching `*/apps.json` | Cloudflare publish only |
| `publish-dois.yml` | daily 04:25 UTC, manual | Python tests → plan batches (`ZENODO_MAX_PUBLICATIONS`, default 16) → matrix publish → enrich DOIs → Cloudflare → commit |
| `audit-doi-licenses.yml` | Sundays 05:00 UTC, manual | read-only audit, uploads JSON artifact |

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
python -m scripts.zenodo_catalog plan --catalog-root . --max-artifacts 16
```

Run Python scripts as modules (`python -m scripts.<name>`) from the repo root.

## Things that touch production — ask first

- `npm run publish:cloudflare` updates the public Worker.
- `python -m scripts.zenodo_catalog publish` mints permanent production DOIs.
- Pushing to `main` triggers `publish-catalog.yml`.

Tests must not hit the network; existing tests stub `fetch`/`requests`.
Follow that pattern when adding tests.
