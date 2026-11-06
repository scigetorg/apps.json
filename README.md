# Neurodesk apps catalog

This directory is the root of the future public `Neurodesk/apps.json` GitHub
repository. The active domain is `neuroimaging`, sourced from
`Neurodesk/neurocontainers`. Catalogs are generated from
configured source repositories; edit their recipe metadata and release files,
not generated `apps.json` files.

## Workflows

```
                     neurocontainers repo                      Zenodo (production)
                (releases/*.json, recipes/*/build.yaml)
                              │
 ┌─ consolidate.yml ── daily 02:25 UTC ───────────────────────────────────────────┐
 │  clone sources ─► build catalog from release JSON (domains from build.yaml)     │
 │                   ├─ DOIs:     look up published records ◄──────────── Zenodo   │
 │                   └─ licenses: every image, from recipe git history             │
 │                                → newest commit declaring its version            │
 │  ─► write <domain>/apps.json + manifest.json                                    │
 │  ─► commit + push main ─► publish to Cloudflare Worker                          │
 └─────────────────────────────────────────────────────────────────────────────────┘
                              │ apps.json on main
                              ▼
 ┌─ publish-dois.yml ── daily 04:25 UTC ──────────────────────────────────────────┐
 │  plan:    unit tests ─► licensed images in apps.json with no Zenodo record      │
 │           ─► split into ≤ 6 batches (none missing → stop)                       │
 │  publish: 6 parallel jobs, per image:                                           │
 │           recipe-history license == apps.json license?                          │
 │             no  ─► log FAILED, continue with the next image                     │
 │             yes ─► Zenodo draft ─► stream S3 SIF ─► publish ─────────► DOI      │
 │  (the DOI enters apps.json at the next 02:25 consolidate)                       │
 └─────────────────────────────────────────────────────────────────────────────────┘

 ┌─ audit-doi-licenses.yml ── Sundays 05:00 UTC (read-only) ──────────────────────┐
 │  list Zenodo records ─► keep software records whose title is in apps.json       │
 │  ─► Zenodo licenses vs apps.json licenses (all of them, any order)              │
 │        verified │ verified_legacy │ mismatch │ no_catalog_license               │
 │  ─► JSON report artifact; job fails on any mismatch                             │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

`consolidate` and `publish-dois` share the `apps-catalog` concurrency group, so
they never run at the same time. The `Test` workflow (not shown) runs the unit
tests on pull requests and code pushes.

## Naming and layout

A **domain ID** is a stable lowercase slug: letters, digits, and single
hyphens, beginning with a letter. Examples are `neuroimaging` and
`microscopy`. The ID is the directory name in the catalog repository.
Keep it stable when a containers repository is renamed. Tool and app names
remain scoped inside each domain's `apps.json`.

`domains.json` maps each ID to a nonempty `source_repositories` list in
`owner/repository` form. It is the allowed domain list. A source recipe declares
`domains: [neuroimaging]` in `recipes/<recipe>/build.yaml`; retired recipes
declare domains in `workflows/retired_recipes.yaml`. Every release must resolve
to one of those declarations. The source must be listed for every domain it
declares.

```text
domains.json
neuroimaging/apps.json       # generated catalog
neuroimaging/manifest.json   # generated image count and source commits
microscopy/apps.json         # generated after its source is configured
microscopy/manifest.json     # generated
```

Microscopy is an example only. To add it later, list the source repositories
under `microscopy` in `domains.json` before recipes declare that domain. An
empty configured domain publishes an empty catalog until its
first app arrives.

## Consolidation rules

Each domain's `apps.json` contains releases from configured repositories whose
current recipe or retirement metadata names that domain. All versions move
together when a declaration changes. A recipe may name multiple domains. The
generated catalog omits the `domains` field because its path identifies the
domain. Duplicate app or image identities across repositories fail generation.
Each generated app carries the exact `image` name of its SIF, including GUI
launchers whose release has no primary app entry.
`show_in_menu` and `show_in_applist` appear only when release JSON explicitly
supplies them, at the app or release level. Existing license values follow the
same image build across domain moves. DOI values follow the same build when a
Zenodo lookup is unavailable.

Deleting a release JSON retires that version from the next catalog while
retaining other versions. Deleting the last release produces an empty catalog.
`manifest.json` records the image count and source commits.

### Where licenses come from

An image `<image>_<build date>` comes from `releases/<tool>/<release>.json`, and
the release file name is the recipe version. Its license is the `copyright` of
the **newest commit of `recipes/<recipe>/build.yaml` whose `version:` equals that
release** (`scripts/recipe_history.py`). The recipe is the release JSON's
`recipe`, else the tool name (`<tool>_arm64` falls back to `<tool>`); deleted
recipes are still found in history. Every catalog run resolves every image this
way. It is local git on a full clone of the source repository, with no registry
or network calls, so a license fixed in a recipe without a version bump reaches
`apps.json` on the next run, with no rebuild. The `license` list shows catalogue
IDs such as `MIT` as the ID, and custom terms (`other-*`, `LicenseRef-…`) by their
`name`, e.g. `FreeSurfer Software License Agreement`; their name and URL are also
in `license_details`.

This states what the recipe declares for that version, not what the image was
built with: a build made before a license fix shows the fixed license. The
license in `apps.json` is kept when no commit declares the version or that
commit has no `copyright`. That mostly happens for images built before
2025-03-27, which came from `build.sh` recipes. An image with neither has no
license in `apps.json`; no license is borrowed from another version, because
licenses can change between versions. The catalog run logs these images.

### DOIs and the audit

A separate DOI workflow publishes missing SIF records to production Zenodo. The
next catalog run reads this account's published records once and adds their
DOIs to every launcher for the image. A Zenodo lookup failure leaves existing
catalog DOIs in place and does not stop catalog generation. Before publication
the workflow reads the license from recipe history the same way, validates it
against Zenodo's live license catalog, and fails if the `apps.json` licenses
differ. Images whose recipe history has no license for their version are
skipped by the plan, with the reason in its log. The DOI description links the
recipe commit for that version.

A weekly read-only audit compares each published Zenodo record's licenses with
the licenses `apps.json` lists for the same image, ignoring order and legacy ID
spellings. Records published by neurocommand's old pipeline carry one license
each, and it is often not a real statement: that pipeline sent `other-at` when it
could not read the recipe, `other-open` for a name-only entry, `other-closed` for
matlab, nothing at all when the recipe had no `copyright` (Zenodo then shows its
default, `cc-by-4.0`), and otherwise only the first license of the current recipe.
Any one-license record that differs from `apps.json` is therefore reported as
`verified_legacy` with its reason (placeholder category, Zenodo's default, a
subset of `apps.json`, or the first license of the recipe at publish time), not
as a failure, and the backfill replaces it with the licenses `apps.json` takes
from recipe history. A record with several licenses that differ from `apps.json`
can only have come from the records API, and is a `mismatch` to review. Images without
a catalog license are reported as `no_catalog_license`. Records for images
removed from the catalog are skipped. The audit uploads a JSON report and fails
on mismatches. It does not read recipes; the catalog run does that every night.
A Zenodo failure does not block catalog updates. These checks verify agreement
between the recipe, catalog, and Zenodo; they cannot determine whether the
recipe described the upstream legal terms correctly.

For DOI publication, records are created through Zenodo's records API, and
every `copyright` entry in the recipe becomes its own license on the
record. A `license` must be an ID in Zenodo's live license catalogue (deprecated
SPDX IDs such as `GPL-3.0` map to `gpl-3.0-only`). List multiple licenses as
separate `copyright` entries; an SPDX expression such as `MIT OR Apache-2.0` is
not one ID. Custom terms (a `name` only, an `other-*` category such as
`other-closed` or `other-nc`, or a
`LicenseRef-…` ID) are published under their `name` with a link to their `url`,
so they need both. `zenodo_license` is no longer used and is ignored.

## Backfill legacy license lists

Records published before the records API carry one license each. The manual
**Backfill Zenodo license lists** workflow edits the audit's `verified_legacy`
records so they list every license from `apps.json`. It changes only the
license list: the record, DOI, version and files stay the same. It is a dry run
unless `apply` is checked; `limit` and `title` narrow it. Suggested order: dry
run, then `apply` with `limit: 5`, check those records on zenodo.org, then a full
run. Re-running is safe because finished records are no longer `verified_legacy`.
Locally:

```bash
ZENODO_TOKEN=... python -m scripts.backfill_zenodo_rights --catalog-root .            # dry run
ZENODO_TOKEN=... python -m scripts.backfill_zenodo_rights --catalog-root . --apply --limit 5
```

Set `ZENODO_API_ROOT=https://sandbox.zenodo.org/api` with a sandbox token to try it there first.

## Enable the schedule

1. Create a public `Neurodesk/apps.json` repository with a `main` branch and
   place the **contents** of this directory at its root, including
   `.github/workflows/` and `neuroimaging/apps.json`. For a local checkout,
   use `cp -a catalog/apps-json-repo/. /path/to/apps.json/`.
2. Create a production `zenodo.org` access token with `deposit:write` and
   `deposit:actions` scopes, and save it as the `ZENODO_TOKEN` Actions secret in
   `Neurodesk/apps.json`. The workflow uses it to list and publish depositions.
   A sandbox Zenodo token is separate and will not work for production Zenodo.
   Each run publishes every missing SIF, newest first, split across at most 6
   parallel jobs. Images `apps.json` cannot license yet (no license, or custom
   terms without `license_details`) are skipped and listed in the plan log, not
   failed. A job starts no new upload after 5 hours, so it is not killed
   mid-upload; the rest waits for the next run.
3. Update the website's app catalog source to
   `https://apps-json.<your-workers-subdomain>.workers.dev/neuroimaging/apps.json`
   after configuring the Cloudflare publishing described below.
   Website consumers should read the catalog directly and use
   `show_in_applist` to select visible entries. The catalog workflow sends
   no dispatch event and needs no website token.
4. Run **Consolidate apps catalog**, then **Publish apps catalog DOIs** manually
   with `limit: 1` first. Check that record on zenodo.org (licenses, file, DOI)
   before the first full run, because DOIs are permanent. Check the files under `neuroimaging/` and website consumption before
   archiving `neurocommand` or disabling its old queue.

The catalog workflow runs daily at 02:25 UTC; DOI publication runs at 04:25 UTC.
The DOI license audit runs Sundays at 05:00 UTC.
The catalog workflow commits generated files using the repository `GITHUB_TOKEN`. GitHub may
disable scheduled workflows in public
repositories after a long period without activity; check that the schedule
remains enabled.

## Retire an image

Delete its versioned release JSON in the source repository. The next catalog
run removes its launchers from `apps.json`, even when it was the final release.
The DOI planner no longer selects the removed image. A previously published DOI
remains on Zenodo and is omitted from the catalog license audit.

The SIF is removed on a timer, not by an event. Neurocontainers'
`cleanup-stale-containers` runs daily at 11:25 UTC, after the 02:25 catalog run,
and keeps any SIF named in a domain `apps.json`, a source release JSON, or signed
CVMFS. Unreferenced SIFs older than 7 days are deleted from Nectar and S3.

## Publish on Cloudflare Workers

The workflow commits the generated catalogs, then publishes every configured
domain as a Worker static asset at `/<domain>/apps.json`. If the push fails,
nothing is published. The GitHub
repository remains the source of truth. Cloudflare serves the latest deployed
catalog.

1. In Cloudflare, find your account ID and your account's `workers.dev`
   subdomain. Create an account-scoped API token with **Workers Scripts: Edit**
   permission for that account.
2. In this repository's GitHub Actions settings, add `CLOUDFLARE_API_TOKEN` as
   a secret and `CLOUDFLARE_ACCOUNT_ID` as a variable. Optionally set the
   `CLOUDFLARE_WORKER_NAME` variable; it defaults to `apps-json`.
3. Run **Consolidate apps catalog** manually. The publishing step creates or
   updates the Worker and enables its `workers.dev` address. Check
   `https://<worker-name>.<your-workers-subdomain>.workers.dev/neuroimaging/apps.json`.

The direct upload uses Cloudflare's [asset upload session API](https://developers.cloudflare.com/workers/static-assets/direct-upload/)
and uploads changed files only. Assets are served as `application/json` with
`Access-Control-Allow-Origin: *` for website clients. The publishing command
checks each public URL against the generated catalog after deployment. For local publishing,
set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, then run
`npm run publish:cloudflare`. Publishing requires network access and changes the
public Worker.

## Local checks

Use Node.js 24.0.0, then run `npm ci` and `npm test`. The DOI helper tests use
Python with `requests` and `pyyaml`: `python -m unittest discover -s test -p 'test_*.py'`.
The `Test` workflow runs both suites, without secrets, on pull requests and on
code pushes to `main`.
For a local consolidation of all configured domains, run:

```bash
node scripts/workflow.mjs checkout
npm run consolidate
```

The clone step writes to ignored `.catalog-sources/<repository>/` directories.
