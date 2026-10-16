import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { buildCatalog, consolidate, makeFiles, readRetiredImages } from "../scripts/catalog.mjs";
import { retireDomain } from "../scripts/retire.mjs";

const commit = "a".repeat(40);
const releases = [
  { path: "releases/demo/1.0.json", data: { apps: {
    "demo 1.0": { version: "20260921", exec: "" },
    "viewerGUI-demo 1.0": { version: "20260921", exec: "viewer" },
  }, categories: ["visualization"] } },
  { path: "releases/other/2.0.json", data: { apps: {
    "other 2.0": { version: "20260922", exec: "" },
  }, categories: ["analysis"] } },
];

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "neurodesk-retire-"));
  const domainRoot = join(root, "neuroimaging");
  const sourceRoot = join(root, "source");
  await mkdir(domainRoot);
  for (const release of releases) {
    const file = join(sourceRoot, release.path);
    await mkdir(join(file, ".."), { recursive: true });
    await writeFile(file, JSON.stringify(release.data));
  }
  const catalog = buildCatalog(releases, { catalog: {} });
  const files = await makeFiles(catalog, commit, releases);
  for (const [name, content] of Object.entries(files)) {
    await writeFile(join(domainRoot, name), content);
  }
  return { root, domainRoot, sourceRoot };
}

test("retires a shared image, preserves releases and unrelated entries, and survives rebuild", async () => {
  const { root, domainRoot, sourceRoot } = await fixture();
  const originalOther = JSON.parse(await readFile(join(domainRoot, "apps.json"), "utf8")).other;
  const result = await retireDomain({ root, domain: "neuroimaging", image: "demo_1.0_20260921", sourceRoot, sourceCommit: commit });
  assert.equal(result.changed, true);
  assert.deepEqual(result.affected.map((item) => item.name), ["demo 1.0", "viewerGUI-demo 1.0"]);
  const catalog = JSON.parse(await readFile(join(domainRoot, "apps.json"), "utf8"));
  for (const app of Object.values(catalog.demo.apps)) {
    assert.equal(app.show_in_menu, false);
    assert.equal(app.show_in_applist, false);
  }
  assert.deepEqual(catalog.other, originalOther);
  assert.deepEqual([...await readRetiredImages(domainRoot)], ["demo_1.0_20260921"]);
  const log = await readFile(join(domainRoot, "logs.txt"), "utf8");
  assert.equal(log, "other_2.0_20260922 categories:analysis,\n");
  const manifest = JSON.parse(await readFile(join(domainRoot, "manifest.json"), "utf8"));
  assert.equal(manifest.entries, 1);
  assert.equal(manifest.sha256, createHash("sha256").update(log).digest("hex"));
  assert.equal(manifest.source_commit, commit);
  const again = await retireDomain({ root, domain: "neuroimaging", image: "demo_1.0_20260921", sourceRoot, sourceCommit: commit });
  assert.equal(again.changed, false);
  const rebuilt = buildCatalog(releases, { catalog }, await readRetiredImages(domainRoot));
  const rebuiltFiles = await makeFiles(rebuilt, commit, releases, await readRetiredImages(domainRoot));
  assert.equal(rebuiltFiles["apps.json"], await readFile(join(domainRoot, "apps.json"), "utf8"));
  assert.equal(rebuiltFiles["logs.txt"], log);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ hits: { hits: [] } }) });
  try {
    const daily = await consolidate({ root: domainRoot, sourceRoot, sourceCommit: commit, doiDelayMs: 0, zenodoToken: "test-token" });
    assert.equal(daily["apps.json"], rebuiltFiles["apps.json"]);
    assert.equal(daily["logs.txt"], log);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("rejects missing, mismatched, or invalid retirement state before changing files", async () => {
  const { root, domainRoot, sourceRoot } = await fixture();
  const catalogBefore = await readFile(join(domainRoot, "apps.json"), "utf8");
  await assert.rejects(retireDomain({ root, domain: "neuroimaging", image: "missing_1.0_20260921", sourceRoot, sourceCommit: commit }), /absent from release JSON/);
  await assert.rejects(retireDomain({ root, domain: "neuroimaging", image: "demo_1.0_20260921", sourceRoot, sourceCommit: "b".repeat(40) }), /does not match/);
  assert.equal(await readFile(join(domainRoot, "apps.json"), "utf8"), catalogBefore);
  await writeFile(join(domainRoot, "retired-images.json"), '{"images":["demo_1.0_20260921","demo_1.0_20260921"]}');
  await assert.rejects(readRetiredImages(domainRoot), /Invalid or duplicate/);
});

test("the final retired image produces a valid empty inventory", async () => {
  const only = [releases[0]];
  const retired = new Set(["demo_1.0_20260921"]);
  const catalog = buildCatalog(only, { catalog: {} }, retired);
  const files = await makeFiles(catalog, commit, only, retired);
  assert.equal(files["logs.txt"], "");
  const manifest = JSON.parse(files["manifest.json"]);
  assert.equal(manifest.entries, 0);
  assert.equal(manifest.sha256, createHash("sha256").update("").digest("hex"));
});
