import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  applyMetadata, buildCatalog, collectDoiMetadata, collectLicenseMetadata,
  makeFiles, readReleases, readSourceDomains,
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

test("DOI and build-time recipe licenses enrich every launcher", async () => {
  const sourceRoot = await mkdtemp(join(tmpdir(), "catalog-license-"));
  await mkdir(join(sourceRoot, "recipes", "demo"), { recursive: true });
  await writeFile(join(sourceRoot, "recipes", "demo", "build.yaml"),
    "domains: [neuroimaging]\ncopyright:\n  - license: Apache-2.0\n");
  const catalog = buildCatalog(rows, { catalog: {} });
  {
    const sourceRoots = { demo: Object.fromEntries(Object.keys(catalog.demo.apps)
      .map((name) => [name, { root: sourceRoot, recipe: "demo", repository }])) };
    const [dois, licenses] = await Promise.all([
      collectDoiMetadata(catalog, rows, { "demo_1.0_20260921": "10.123/demo" }),
      collectLicenseMetadata(catalog, sourceRoots, { resolve: async (titles, sources) => {
        assert.deepEqual(titles, ["demo_1.0_20260921"]);
        assert.deepEqual(sources, { "demo_1.0_20260921": [repository] });
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
  }
});

test("the same image reuses its catalog license and custom terms", async () => {
  const catalog = buildCatalog(rows, { catalog: {} });
  const sources = { demo: Object.fromEntries(Object.keys(catalog.demo.apps)
    .map((name) => [name, { recipe: "demo" }])) };
  const details = [{ license: "other-closed", name: "Research terms",
    url: "https://example.org/terms" }];
  const previousByImage = new Map([["demo_1.0_20260921", {
    license: ["other-closed"], license_details: details,
  }]]);
  const result = await collectLicenseMetadata(catalog, sources, {
    previousByImage, resolve: async () => {
      assert.fail("Existing image should not require a recipe lookup");
    },
  });
  const enriched = applyMetadata(catalog, {}, result);
  for (const app of Object.values(enriched.demo.apps)) {
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
      assert.deepEqual(titles, ["demo_1.0_20261010"]);
      return { resolved: { "demo_1.0_20261010": {
        license: ["Apache-2.0"], license_details: [],
      } }, errors: {} };
    },
  });
  assert.deepEqual(applyMetadata(catalog, {}, licenses).demo.apps["demo 1.0"].license,
    ["Apache-2.0"]);
});

test("changed release license is checked against the build-time recipe", async () => {
  const changed = structuredClone(rows);
  changed[0].data.apps["demo 1.0"].license = ["Apache-2.0"];
  const catalog = buildCatalog(changed, { catalog: {} });
  const previousByImage = new Map([["demo_1.0_20260921", {
    license: ["MIT"], license_details: [],
  }]]);
  const licenses = await collectLicenseMetadata(catalog,
    { demo: { "demo 1.0": { recipe: "demo" } } }, {
      previousByImage,
      resolve: async (titles) => {
        assert.deepEqual(titles, ["demo_1.0_20260921"]);
        return { resolved: { "demo_1.0_20260921": {
          license: ["Apache-2.0"], license_details: [],
        } }, errors: {} };
      },
    });
  assert.deepEqual(applyMetadata(catalog, {}, licenses).demo.apps["demo 1.0"].license,
    ["Apache-2.0"]);
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
