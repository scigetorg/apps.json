# NeuroDesk apps catalog

This directory is the root of the future public `NeuroDesk/apps.json` GitHub
repository. The active domain is `neuroimaging`, sourced from
`NeuroDesk/neurocontainers`. Its seed catalog at `neuroimaging/apps.json` is
copied from `neurocommand/neurodesk/apps.json` to preserve visibility choices.

## Naming and layout

A **domain ID** is a stable lowercase slug: letters, digits, and single
hyphens, beginning with a letter. Examples are `neuroimaging` and
`microscopy`. The ID is the directory name and the value in dispatch payloads.
Keep it stable when a containers repository is renamed. Tool and app names
remain scoped inside each domain's `apps.json`.

`domains.json` maps each domain ID to its `source_repository` in
`owner/repository` form. An optional `dispatch_repository` receives an event
when that domain's catalog changes. The workflow reads this map; it does not
derive a repository name from a domain ID.

```text
domains.json
neuroimaging/apps.json
neuroimaging/applist.json    # generated
neuroimaging/logs.txt        # generated
neuroimaging/manifest.json   # generated
microscopy/apps.json         # after its source repository is configured
microscopy/applist.json      # generated
microscopy/logs.txt          # generated
microscopy/manifest.json     # generated
```

Microscopy is an example only. Its containers repository has not been created,
so it is not in `domains.json` and cannot interrupt the daily neuroimaging run.
To add it later, create the source repository with `releases/*/*.json`, add a
`microscopy` entry to `domains.json`, and optionally seed
`microscopy/apps.json` with existing visibility choices. The workflow creates
the domain directory and missing generated files on its first run.

Each `apps.json` contains release apps with `show_in_menu` and
`show_in_applist` at the app version level. Existing values are retained; new
app versions default to visible. `logs.txt` includes all release images,
including hidden and GUI-only releases. `manifest.json` records the log's
SHA-256, entry count, and source commit. `applist.json` uses the existing
website `{ "list": [...] }` format with visible primary apps.

The workflow clones each configured containers repository, consolidates its
release JSON, and searches Zenodo for missing DOI values by exact image and
build title. DOI and recipe license lookups run concurrently within each
domain. A release DOI or license takes precedence over retained values; recipe
license IDs fill missing values. Historical releases should state their
license when it differs from the current recipe.

## Enable the schedule

1. Create a public `NeuroDesk/apps.json` repository with a `main` branch and
   place the **contents** of this directory at its root, including
   `.github/workflows/`. For a local checkout, use
   `cp -a catalog/apps-json-repo/. /path/to/apps.json/`.
2. Set the Actions secret `SITE_DISPATCH_TOKEN` to a GitHub App installation
   token or fine-grained personal access token that can dispatch to
   `NeuroDesk/neurodesk.github.io`. For a fine-grained token, grant that
   repository Contents: write. The workflow's own `GITHUB_TOKEN` commits the
   generated files to `NeuroDesk/apps.json`.
3. Update the website app list source in
   `NeuroDesk/neurodesk.github.io/.github/workflows/write-app-json.py` to
   `https://raw.githubusercontent.com/NeuroDesk/apps.json/main/neuroimaging/applist.json`.
   The dispatch payload includes `domain`, `apps_path`, and `applist_path` so
   website consumers can select the correct domain.
4. Run **Consolidate apps catalog** manually, then check the files under
   `neuroimaging/`, the website rebuild, and CVMFS log retrieval before
   archiving `neurocommand` or disabling its old queue.

The workflow runs daily at 02:25 UTC. A failed website dispatch after a
successful commit can be retried manually with `force_site_dispatch: true`.
GitHub may disable scheduled workflows in public repositories after a long
period without activity; check that the schedule remains enabled.

## Local checks

Use Node.js 24.0.0, then run `npm ci` and `npm test`. For a local consolidation
of all configured domains, run:

```bash
node scripts/workflow.mjs checkout
npm run consolidate
```

The clone step writes to ignored `.catalog-sources/<domain>/` directories.
