import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  applyMetadata, buildCatalog, collectDoiMetadata, collectLicenseMetadata,
  describeReadme, makeFiles, readReleases, readSourceDomains,
} from "../scripts/catalog.mjs";

const repository = "Example/containers";
const rows = [{ path: "releases/demo/1.0.json", data: { repository,
  apps: {
    "demo 1.0": { version: "20260921", exec: "" },
    "viewerGUI-demo 1.0": { version: "20260921", exec: "viewer" },
  }, categories: ["visualization"], show_in_menu: false,
} }];

test("only explicit release visibility flags appear in the catalog", () => {
  const previous = { catalog: { demo: { apps: {
    "demo 1.0": { show_in_menu: true, show_in_applist: false },
  } } } };
  const catalog = buildCatalog(rows, previous);
  for (const app of Object.values(catalog.demo.apps)) {
    assert.equal(app.show_in_menu, false);
    assert.equal(Object.hasOwn(app, "show_in_applist"), false);
  }
  assert.equal(catalog.demo.apps["demo 1.0"].image, "demo_1.0");
  assert.equal(catalog.demo.apps["viewerGUI-demo 1.0"].image, "demo_1.0");
  assert.equal(Object.hasOwn(catalog.demo, "domains"), false);
});

test("a GUI-only release remains in the catalog", async () => {
  const gui = [{ path: "releases/demo/2.0.json", data: { repository,
    apps: { "viewerGUI-demo 2.0": { version: "20260922", exec: "viewer" } },
    categories: ["visualization"],
  } }];
  const catalog = buildCatalog(gui, { catalog: {} });
  const files = await makeFiles(catalog, { [repository]: "a".repeat(40) }, gui);
  assert.deepEqual(Object.keys(files), ["apps.json", "manifest.json"]);
  assert.equal(catalog.demo.apps["viewerGUI-demo 2.0"].image, "demo_2.0");
  assert.equal(Object.hasOwn(catalog.demo.apps["viewerGUI-demo 2.0"], "show_in_menu"), false);
});

test("deleting one release removes its image and launchers but retains other versions", async () => {
  const newer = { path: "releases/demo/2.0.json", data: { repository,
    apps: { "demo 2.0": { version: "20260922", exec: "" } },
    categories: ["visualization"],
  } };
  const after = buildCatalog([newer], { catalog: buildCatalog([...rows, newer], { catalog: {} }) });
  assert.deepEqual(Object.keys(after.demo.apps), ["demo 2.0"]);
  const files = await makeFiles(after, { [repository]: "a".repeat(40) }, [newer]);
  assert.equal(JSON.parse(files["manifest.json"]).entries, 1);
});

test("empty domain produces an empty catalog and manifest", async () => {
  const files = await makeFiles(buildCatalog([], { catalog: {} }),
    { [repository]: "a".repeat(40) }, []);
  assert.deepEqual(JSON.parse(files["apps.json"]), {});
  assert.deepEqual(Object.keys(files), ["apps.json", "manifest.json"]);
  const manifest = JSON.parse(files["manifest.json"]);
  assert.equal(manifest.entries, 0);
  assert.equal(Object.hasOwn(manifest, "logs_sha256"), false);
});

test("DOI and recipe-history licenses enrich every launcher", async () => {
  const sourceRoot = await mkdtemp(join(tmpdir(), "catalog-license-"));
  const catalog = buildCatalog(rows, { catalog: {} });
  const sourceRoots = { demo: Object.fromEntries(Object.keys(catalog.demo.apps)
    .map((name) => [name, { root: sourceRoot, recipe: "demo", repository }])) };
  const [dois, licenses] = await Promise.all([
    collectDoiMetadata(catalog, rows, { "demo_1.0_20260921": "10.123/demo" }),
    collectLicenseMetadata(catalog, sourceRoots, { resolve: async (titles) => {
      // One lookup per image (not per launcher), with its source clone.
      assert.deepEqual(titles, { "demo_1.0_20260921": { repository, root: sourceRoot } });
      return { resolved: { "demo_1.0_20260921": {
        license: ["MIT"], license_details: [],
      } }, errors: {} };
    } }),
  ]);
  const enriched = applyMetadata(catalog, dois, licenses);
  for (const app of Object.values(enriched.demo.apps)) {
    assert.equal(app.doi, "https://doi.org/10.123/demo");
    assert.deepEqual(app.license, ["MIT"]);
  }
});

test("recipe history replaces a stored license; the stored one stays without history", async () => {
  const catalog = buildCatalog(rows, { catalog: {} });
  const sources = { demo: Object.fromEntries(Object.keys(catalog.demo.apps)
    .map((name) => [name, { recipe: "demo" }])) };
  const details = [{ license: "other-closed", name: "Research terms",
    url: "https://example.org/terms" }];
  const previousByImage = new Map([["demo_1.0_20260921", {
    license: ["other-closed"], license_details: details,
  }]]);
  const fixed = { license: ["MIT", "other-closed"], license_details: details };
  let result = await collectLicenseMetadata(catalog, sources, {
    previousByImage,
    resolve: async () => ({ resolved: { "demo_1.0_20260921": fixed }, errors: {} }),
  });
  let app = applyMetadata(catalog, {}, result).demo.apps["demo 1.0"];
  assert.deepEqual(app.license, ["MIT", "other-closed"]);
  // No commit declares the version (or it has no copyright): keep what apps.json has.
  for (const answer of [
    { resolved: {}, errors: { "demo_1.0_20260921": "No commit declares version '1.0'" } },
    { resolved: { "demo_1.0_20260921": { license: [], license_details: [] } }, errors: {} },
  ]) {
    result = await collectLicenseMetadata(catalog, sources, { previousByImage, resolve: async () => answer });
    app = applyMetadata(catalog, {}, result).demo.apps["demo 1.0"];
    assert.deepEqual(app.license, ["other-closed"]);
    assert.deepEqual(app.license_details, details);
  }
});

test("a new build does not inherit the previous build's license or DOI", async () => {
  const prior = buildCatalog(rows, { catalog: {} });
  prior.demo.apps["demo 1.0"].license = ["MIT"];
  prior.demo.apps["demo 1.0"].doi = "https://doi.org/10.123/old";
  const next = structuredClone(rows);
  next[0].data.apps["demo 1.0"].version = "20261010";
  const catalog = buildCatalog(next, { catalog: prior }, new Map([
    [JSON.stringify([repository, "demo", "demo 1.0"]), prior.demo.apps["demo 1.0"]],
  ]));
  assert.equal(Object.hasOwn(catalog.demo.apps["demo 1.0"], "license"), false);
  assert.equal(Object.hasOwn(catalog.demo.apps["demo 1.0"], "doi"), false);
  const sources = { demo: { "demo 1.0": { recipe: "demo" } } };
  const licenses = await collectLicenseMetadata(catalog, sources, {
    previousByImage: new Map([["demo_1.0_20260921", {
      license: ["MIT"], license_details: [],
    }]]),
    resolve: async (titles) => {
      assert.deepEqual(Object.keys(titles), ["demo_1.0_20261010"]);
      return { resolved: {}, errors: { "demo_1.0_20261010": "No commit declares version '1.0'" } };
    },
  });
  assert.equal(Object.hasOwn(applyMetadata(catalog, {}, licenses).demo.apps["demo 1.0"], "license"),
    false);
});

test("an account DOI replaces an old DOI for the same image", async () => {
  const catalog = buildCatalog(rows, { catalog: {} });
  catalog.demo.apps["demo 1.0"].doi = "https://doi.org/10.5072/zenodo.1";
  const dois = await collectDoiMetadata(catalog, rows,
    { "demo_1.0_20260921": "10.5281/zenodo.2" });
  assert.equal(applyMetadata(catalog, dois, {}).demo.apps["demo 1.0"].doi,
    "https://doi.org/10.5281/zenodo.2");
});

test("recipe domains are mandatory and validated against the registry", async () => {
  const root = await mkdtemp(join(tmpdir(), "catalog-domains-"));
  await mkdir(join(root, "releases", "demo"), { recursive: true });
  await mkdir(join(root, "recipes", "demo"), { recursive: true });
  await writeFile(join(root, "releases", "demo", "1.0.json"), JSON.stringify(rows[0].data));
  const releases = await readReleases(root);
  const registry = { neuroimaging: { source_repositories: [repository] } };
  await writeFile(join(root, "recipes", "demo", "build.yaml"), "domains: [neuroimaging]\n");
  assert.deepEqual((await readSourceDomains(root, releases, registry, repository)).get("demo").domains,
    ["neuroimaging"]);
  await writeFile(join(root, "recipes", "demo", "build.yaml"), "domains: [microsopy]\n");
  await assert.rejects(readSourceDomains(root, releases, registry, repository), /Unknown domain/);
  await writeFile(join(root, "recipes", "demo", "build.yaml"), "categories: [visualization]\n");
  await assert.rejects(readSourceDomains(root, releases, registry, repository), /Missing or invalid domains/);
});

test("recipes supply each tool's description and OpenRecon flag", async () => {
  const root = await mkdtemp(join(tmpdir(), "catalog-details-"));
  const registry = { neuroimaging: { source_repositories: [repository] } };
  for (const tool of ["demo", "scanner", "plain"]) {
    await mkdir(join(root, "releases", tool), { recursive: true });
    await mkdir(join(root, "recipes", tool), { recursive: true });
    await writeFile(join(root, "releases", tool, "1.0.json"), JSON.stringify(rows[0].data));
  }
  await writeFile(join(root, "recipes", "demo", "build.yaml"), [
    "name: demo", 'version: "1.0"', "domains: [neuroimaging]", "variables:", "  upstream: Demo {{ context.version }}",
    "readme: |", "  ----------------------------------", "  ## demo/{{ context.version }} ##",
    "  Packages {{ context.upstream }}", "  for viewing.", "", "  Example:", "  ```", "  demo", "  ```", "",
  ].join("\n"));
  await writeFile(join(root, "recipes", "scanner", "build.yaml"), "domains: [neuroimaging]\n");
  await writeFile(join(root, "recipes", "scanner", "README.md"), "## scanner/2 ##\nRuns on the scanner.\n");
  await writeFile(join(root, "recipes", "scanner", "OpenReconLabel.gpu.json"), "{}");
  await writeFile(join(root, "recipes", "plain", "build.yaml"), "domains: [neuroimaging]\n");
  const metadata = await readSourceDomains(root, await readReleases(root), registry, repository);
  assert.deepEqual(metadata.get("demo").details, { description: "Packages Demo 1.0 for viewing." });
  assert.deepEqual(metadata.get("scanner").details, { description: "Runs on the scanner.", openrecon: true });
  assert.deepEqual(metadata.get("plain").details, {});

  const catalog = applyMetadata(buildCatalog(rows, { catalog: {} }), {}, {},
    { demo: metadata.get("demo").details });
  assert.equal(catalog.demo.description, "Packages Demo 1.0 for viewing.");
  assert.equal(catalog.demo.openrecon, undefined);
  assert.deepEqual(Object.keys(catalog.demo.apps), ["demo 1.0", "viewerGUI-demo 1.0"]);
});

test("a readme without description text yields none", () => {
  assert.equal(describeReadme("## demo/1 ##\nExample:\ndemo"), undefined);
  assert.equal(describeReadme("demo/{{ context.version }}\n{{ context.missing }} stays", {}), "{{ context.missing }} stays");
  assert.equal(describeReadme(undefined), undefined);
});
