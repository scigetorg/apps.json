# Neurodesk apps catalog

This directory is the root of the future public `Neurodesk/apps.json` GitHub
repository. The active domain is `neuroimaging`, sourced from
`Neurodesk/neurocontainers`. Catalogs and inventories are generated from
configured source repositories; edit their recipe metadata and release files,
not generated `apps.json` files.

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
neuroimaging/logs.txt        # generated CVMFS inventory
neuroimaging/manifest.json   # generated log checksum and source commits
microscopy/apps.json         # generated after its source is configured
microscopy/logs.txt          # generated
microscopy/manifest.json     # generated
```

Microscopy is an example only. To add it later, list the source repositories
under `microscopy` in `domains.json` before recipes declare that domain. An
empty configured domain publishes an empty catalog and inventory until its
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
supplies them, at the app or release level. Existing DOI and license values
follow an app across domain moves when release metadata cannot supply them.

`logs.txt` contains every release image and build in the domain, including
GUI-only releases. Deleting a release JSON retires that version from the next
catalog and log while retaining other versions. `manifest.json` records the
log's SHA-256, image count, and source commits. The catalog workflow builds
domain catalogs and extracts licenses from current recipes. A separate DOI
workflow publishes missing SIF records to production Zenodo and adds their DOIs
to every launcher for the image. Before publication it validates the license
against Zenodo's live license catalog and checks the recipe at the image's
OCI build revision. It fails if `apps.json` licenses differ from that recipe.
The DOI description records every declared license and the build recipe revision.
A weekly read-only audit compares published Zenodo licenses and catalog licenses
with those same build-time recipes, and uploads a JSON report. A Zenodo failure
does not block catalog updates. Historical releases should state their license
when it differs from the current recipe.

## Enable the schedule

1. Create a public `Neurodesk/apps.json` repository with a `main` branch and
   place the **contents** of this directory at its root, including
   `.github/workflows/` and `neuroimaging/apps.json`. For a local checkout,
   use `cp -a catalog/apps-json-repo/. /path/to/apps.json/`.
2. Create a production `zenodo.org` access token with `deposit:write` and
   `deposit:actions` scopes, and save it as the `ZENODO_TOKEN` Actions secret in
   `Neurodesk/apps.json`. The workflow uses it to list and publish depositions.
   A sandbox Zenodo token is separate and will not work for production Zenodo.
   By default, at most 16 missing SIFs are published per run, newest first;
   set the `ZENODO_MAX_PUBLICATIONS` repository variable to change that limit.
3. Update the website's app catalog source to
   `https://apps-json.<your-workers-subdomain>.workers.dev/neuroimaging/apps.json`
   after configuring the Cloudflare publishing described below.
   Website consumers should read the catalog directly and use
   `show_in_applist` to select visible entries. The catalog workflow sends
   no dispatch event and needs no website token.
4. Run **Consolidate apps catalog**, then **Publish apps catalog DOIs** manually.
   Check the files under `neuroimaging/`, website consumption, and CVMFS log
   retrieval before
   archiving `neurocommand` or disabling its old queue.

The catalog workflow runs daily at 02:25 UTC; DOI publication runs at 04:25 UTC.
The DOI license audit runs Sundays at 05:00 UTC.
Both commit generated files using the repository `GITHUB_TOKEN`. GitHub may
disable scheduled workflows in public
repositories after a long period without activity; check that the schedule
remains enabled.

## Retire an image

Delete its versioned release JSON in the source repository. After the daily
catalog run publishes the new domain logs, the CVMFS sync can remove the image
if no domain still references it. The object-store cleaner applies its retention
period after the image is absent from all published inventories and source
release files. Published Docker registry cleanup is a separate task.

## Publish on Cloudflare Workers

The workflow publishes every configured domain as a Worker static asset at
`/<domain>/apps.json` and commits the generated catalogs. The GitHub
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
Python with `requests`: `python -m unittest discover -s test -p test_zenodo_catalog.py`.
For a local consolidation of all configured domains, run:

```bash
node scripts/workflow.mjs checkout
npm run consolidate
```

The clone step writes to ignored `.catalog-sources/<repository>/` directories.
