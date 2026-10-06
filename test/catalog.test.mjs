import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  applyMetadata,
  buildCatalog,
  collectDoiMetadata,
  collectLicenseMetadata,
  consolidate,
  makeFiles,
  readReleases,
} from "../scripts/catalog.mjs";

const release = {
  path: "releases/demo/1.0.json",
  data: {
    apps: {
      "demo 1.0": { version: "20260921", exec: "" },
      "viewerGUI-demo 1.0": { version: "20260921", exec: "viewer" },
    },
    categories: ["visualization"],
  },
};

test("retains per-version visibility and generates a complete CVMFS inventory", async () => {
  const previous = { catalog: { demo: { apps: {
    "demo 1.0": { show_in_applist: false },
    "viewerGUI-demo 1.0": { show_in_menu: false },
  } } } };
  const catalog = buildCatalog([release], previous);
  assert.equal(catalog.demo.apps["demo 1.0"].show_in_applist, false);
  assert.equal(catalog.demo.apps["viewerGUI-demo 1.0"].show_in_menu, false);
  const files = await makeFiles(catalog, "source-sha", [release]);
  assert.equal(files["logs.txt"], "demo_1.0_20260921 categories:visualization,\n");
  assert.deepEqual(JSON.parse(files["applist.json"]).list, []);
  assert.equal(JSON.parse(files["manifest.json"]).entries, 1);
});

test("a GUI-only release remains in the CVMFS inventory", async () => {
  const guiOnly = {
    path: "releases/demo/2.0.json",
    data: { apps: { "viewerGUI-demo 2.0": { version: "20260922", exec: "viewer" } }, categories: ["visualization"] },
  };
  const catalog = buildCatalog([guiOnly], { catalog: {} });
  const files = await makeFiles(catalog, "source-sha", [guiOnly]);
  assert.equal(files["logs.txt"], "demo_2.0_20260922 categories:visualization,\n");
});

test("an explicit image name is used in the CVMFS log and website list", async () => {
  const renamed = {
    path: "releases/demo/3.0.json",
    data: { apps: { "demo 3.0": { version: "20260923", image: "renamed_demo_3.0" } }, categories: ["visualization"] },
  };
  const catalog = buildCatalog([renamed], { catalog: {} });
  const files = await makeFiles(catalog, "source-sha", [renamed]);
  assert.equal(files["logs.txt"], "renamed_demo_3.0_20260923 categories:visualization,\n");
  assert.equal(JSON.parse(files["applist.json"]).list[0].application, "renamed_demo_3.0_20260923");
});

test("DOI and recipe license enrich the same app versions", async () => {
  const sourceRoot = await mkdtemp(join(tmpdir(), "neurodesk-catalog-license-"));
  await mkdir(join(sourceRoot, "recipes", "demo"), { recursive: true });
  await writeFile(join(sourceRoot, "recipes", "demo", "build.yaml"), "copyright:\n  - license: MIT\n");
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ hits: { hits: [{
    id: 12, doi: "10.123/demo", metadata: { title: "demo_1.0_20260921" },
  }] } }) });
  try {
    const base = buildCatalog([release], { catalog: {} });
    const [dois, licenses] = await Promise.all([
      collectDoiMetadata(base, [release], 0), collectLicenseMetadata(base, sourceRoot),
    ]);
    const catalog = applyMetadata(base, dois, licenses);
    assert.deepEqual(Object.values(catalog.demo.apps).map((app) => app.doi), [
      "https://doi.org/10.123/demo", "https://doi.org/10.123/demo",
    ]);
    assert.deepEqual(Object.values(catalog.demo.apps).map((app) => app.license), [["MIT"], ["MIT"]]);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("reads release files and writes all generated files", async () => {
  const root = await mkdtemp(join(tmpdir(), "neurodesk-catalog-output-"));
  const sourceRoot = join(root, "neurocontainers");
  await mkdir(join(sourceRoot, "releases", "demo"), { recursive: true });
  await writeFile(join(sourceRoot, release.path), JSON.stringify(release.data));
  await writeFile(join(root, "apps.json"), JSON.stringify({ demo: { apps: {
    "demo 1.0": { show_in_menu: false, doi: "https://doi.org/10.123/demo" },
    "viewerGUI-demo 1.0": { doi: "https://doi.org/10.123/demo" },
  } } }));
  assert.equal((await readReleases(sourceRoot)).length, 1);
  const files = await consolidate({ root, sourceRoot, sourceCommit: "a".repeat(40), doiDelayMs: 0 });
  for (const [name, content] of Object.entries(files)) {
    assert.equal(await readFile(join(root, name), "utf8"), content);
  }
  assert.equal(JSON.parse(files["apps.json"]).demo.apps["demo 1.0"].show_in_menu, false);
});
