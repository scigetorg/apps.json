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

test("a GUI-only release remains in the CVMFS inventory", async () => {
  const gui = [{ path: "releases/demo/2.0.json", data: { repository,
    apps: { "viewerGUI-demo 2.0": { version: "20260922", exec: "viewer" } },
    categories: ["visualization"],
  } }];
  const catalog = buildCatalog(gui, { catalog: {} });
  const files = await makeFiles(catalog, { [repository]: "a".repeat(40) }, gui);
  assert.equal(files["logs.txt"], "demo_2.0_20260922 categories:visualization,\n");
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
  assert.equal(files["logs.txt"], "demo_2.0_20260922 categories:visualization,\n");
});

test("empty domain produces an empty catalog and log", async () => {
  const files = await makeFiles(buildCatalog([], { catalog: {} }),
    { [repository]: "a".repeat(40) }, []);
  assert.deepEqual(JSON.parse(files["apps.json"]), {});
  assert.equal(files["logs.txt"], "");
  assert.equal(JSON.parse(files["manifest.json"]).entries, 0);
});

test("DOI and recipe license enrichment still apply", async () => {
  const sourceRoot = await mkdtemp(join(tmpdir(), "catalog-license-"));
  await mkdir(join(sourceRoot, "recipes", "demo"), { recursive: true });
  await writeFile(join(sourceRoot, "recipes", "demo", "build.yaml"),
    "domains: [neuroimaging]\ncopyright:\n  - license: MIT\n");
  const catalog = buildCatalog(rows, { catalog: {} });
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ hits: { hits: [{
    id: 12, doi: "10.123/demo", metadata: { title: "demo_1.0_20260921" },
  }] } }) });
  try {
    const sourceRoots = { demo: Object.fromEntries(Object.keys(catalog.demo.apps)
      .map((name) => [name, { root: sourceRoot, recipe: "demo" }])) };
    const [dois, licenses] = await Promise.all([
      collectDoiMetadata(catalog, rows, { delayMs: 0, token: "test-token" }),
      collectLicenseMetadata(catalog, sourceRoots),
    ]);
    const enriched = applyMetadata(catalog, dois, licenses);
    for (const app of Object.values(enriched.demo.apps)) {
      assert.equal(app.doi, "https://doi.org/10.123/demo");
      assert.deepEqual(app.license, ["MIT"]);
    }
  } finally { globalThis.fetch = previousFetch; }
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
