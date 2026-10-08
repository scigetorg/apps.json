# Neurodesk apps catalog

This directory is the root of the future public `Neurodesk/apps.json` GitHub
repository. The active domain is `neuroimaging`, sourced from
`Neurodesk/neurocontainers`. The seed `neuroimaging/apps.json` has per-app
`show_in_menu` and `show_in_applist` flags set by a **one-time comparison**
with the historical `cvmfs/applist.json`: an exact `<image>_<builddate>` match
set both flags to `true`; absence set both to `false`. That historical list is
not part of this repository or the scheduled workflow.

## Naming and layout

A **domain ID** is a stable lowercase slug: letters, digits, and single
hyphens, beginning with a letter. Examples are `neuroimaging` and
`microscopy`. The ID is the directory name in the catalog repository.
Keep it stable when a containers repository is renamed. Tool and app names
remain scoped inside each domain's `apps.json`.

`domains.json` maps each ID to its `source_repository` in `owner/repository`
form. The workflow reads this map; it does not derive a repository name from
a domain ID.

```text
domains.json
neuroimaging/apps.json       # seeded visibility; updated by workflow
neuroimaging/logs.txt        # generated CVMFS inventory
neuroimaging/manifest.json   # generated log checksum and source commit
microscopy/apps.json         # generated after its source is configured
microscopy/logs.txt          # generated
microscopy/manifest.json     # generated
```

Microscopy is an example only. Its containers repository has not been created,
so it is not in `domains.json` and cannot interrupt the daily neuroimaging run.
To add it later, create the source repository with `releases/*/*.json` and add
a `microscopy` entry to `domains.json`. The workflow creates the directory
and files on its first run.

## Consolidation rules

Each domain's `apps.json` is consolidated from its containers repository's
release JSON. For matching app names, the existing `show_in_menu` and
`show_in_applist` values in `apps.json` are retained independently. New apps
use flags supplied by release JSON, or default to `true` if none are supplied.
Edit `apps.json` to change visibility after the initial seed. Existing DOI
and license values are retained when release JSON does not supply them.

`logs.txt` contains every release image and build, including hidden and
GUI-only releases. `manifest.json` records the log's SHA-256, entry count,
and source commit. DOI lookups by exact image and build title use the Zenodo
Records API with a bearer token and run alongside local recipe license
extraction. Historical releases should state their license
when it differs from the current recipe.

## Enable the schedule

1. Create a public `Neurodesk/apps.json` repository with a `main` branch and
   place the **contents** of this directory at its root, including
   `.github/workflows/` and `neuroimaging/apps.json`. For a local checkout,
   use `cp -a catalog/apps-json-repo/. /path/to/apps.json/`.
2. Create a Zenodo access token for `zenodo.org` and save it as the
   `ZENODO_TOKEN` Actions secret in `Neurodesk/apps.json`. The workflow uses
   it only for DOI lookup requests. A sandbox Zenodo token is separate and will
   not work for production Zenodo.
3. Update the website's app catalog source to
   `https://raw.githubusercontent.com/Neurodesk/apps.json/main/neuroimaging/apps.json`.
   Website consumers should read the catalog directly and use
   `show_in_applist` to select visible entries. The catalog workflow sends
   no dispatch event and needs no website token.
4. Run **Consolidate apps catalog** manually, then check the files under
   `neuroimaging/`, website consumption, and CVMFS log retrieval before
   archiving `neurocommand` or disabling its old queue.

The workflow runs daily at 02:25 UTC and commits generated files using its
repository `GITHUB_TOKEN`. GitHub may disable scheduled workflows in public
repositories after a long period without activity; check that the schedule
remains enabled.

## Local checks

Use Node.js 24.0.0, then run `npm ci` and `npm test`. For a local consolidation
of all configured domains, set `ZENODO_TOKEN` in your environment and run:

```bash
node scripts/workflow.mjs checkout
npm run consolidate
```

The clone step writes to ignored `.catalog-sources/<domain>/` directories.
