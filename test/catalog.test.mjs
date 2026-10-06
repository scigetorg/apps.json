import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, stat, writeFile } from "node:fs/promises";
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

test("preserves seeded per-app visibility without an applist", async () => {
  const previous = { catalog: { demo: { apps: {
    "demo 1.0": { show_in_menu: false, show_in_applist: false },
    "viewerGUI-demo 1.0": { show_in_menu: true, show_in_applist: true },
  } } } };
  const catalog = buildCatalog([release], previous);
  assert.equal(catalog.demo.apps["demo 1.0"].show_in_applist, false);
  assert.equal(catalog.demo.apps["demo 1.0"].show_in_menu, false);
  assert.equal(catalog.demo.apps["viewerGUI-demo 1.0"].show_in_applist, true);
  assert.equal(catalog.demo.apps["viewerGUI-demo 1.0"].show_in_menu, true);
  const files = await makeFiles(catalog, "source-sha", [release]);
  assert.equal(files["logs.txt"], "demo_1.0_20260921 categories:visualization,\n");
  assert.equal(Object.hasOwn(files, "applist.json"), false);
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
  assert.equal(catalog.demo.apps["viewerGUI-demo 2.0"].show_in_menu, true);
  assert.equal(catalog.demo.apps["viewerGUI-demo 2.0"].show_in_applist, true);
});

test("an explicit image name is used in the CVMFS log", async () => {
  const renamed = {
    path: "releases/demo/3.0.json",
    data: { apps: { "demo 3.0": { version: "20260923", image: "renamed_demo_3.0" } }, categories: ["visualization"] },
  };
  const catalog = buildCatalog([renamed], { catalog: {} });
  const files = await makeFiles(catalog, "source-sha", [renamed]);
  assert.equal(files["logs.txt"], "renamed_demo_3.0_20260923 categories:visualization,\n");
});

test("release flags initialize new apps while seeded choices survive rebuilds", () => {
  const flagged = structuredClone(release);
  flagged.data.apps["demo 1.0"].show_in_menu = false;
  flagged.data.apps["demo 1.0"].show_in_applist = false;
  const initial = buildCatalog([flagged], { catalog: {} });
  assert.equal(initial.demo.apps["demo 1.0"].show_in_menu, false);
  const rebuilt = structuredClone(flagged);
  rebuilt.data.apps["demo 1.0"].version = "20260922";
  rebuilt.data.apps["demo 1.0"].show_in_menu = true;
  rebuilt.data.apps["demo 1.0"].show_in_applist = true;
  const next = buildCatalog([rebuilt], { catalog: initial });
  assert.equal(next.demo.apps["demo 1.0"].show_in_menu, false);
  assert.equal(next.demo.apps["demo 1.0"].show_in_applist, false);
});

test("DOI and recipe license enrich the same app versions", async () => {
  const sourceRoot = await mkdtemp(join(tmpdir(), "neurodesk-catalog-license-"));
  await mkdir(join(sourceRoot, "recipes", "demo"), { recursive: true });
  await writeFile(join(sourceRoot, "recipes", "demo", "build.yaml"), "copyright:\n  - license: MIT\n");
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    assert.equal(options.headers.Authorization, "Bearer test-token");
    assert.equal(url.includes("test-token"), false);
    return { ok: true, json: async () => ({ hits: { hits: [{
      id: 12, doi: "10.123/demo", metadata: { title: "demo_1.0_20260921" },
    }] } }) };
  };
  try {
    const base = buildCatalog([release], { catalog: {} });
    const [dois, licenses] = await Promise.all([
      collectDoiMetadata(base, [release], { delayMs: 0, token: "test-token" }),
      collectLicenseMetadata(base, sourceRoot),
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

test("DOI lookup requires a token and reports forbidden responses", async () => {
  const base = buildCatalog([release], { catalog: {} });
  await assert.rejects(
    collectDoiMetadata(base, [release], { delayMs: 0 }),
    /ZENODO_TOKEN is required/,
  );
  const previousFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async (_url, options) => {
    requests++;
    assert.equal(options.headers.Authorization, "Bearer test-token");
    return { ok: false, status: 403 };
  };
  try {
    await assert.rejects(
      collectDoiMetadata(base, [release], { delayMs: 0, token: "test-token" }),
      /HTTP 403; check ZENODO_TOKEN/,
    );
    assert.equal(requests, 1);
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
  await assert.rejects(stat(join(root, "applist.json")), { code: "ENOENT" });
});
