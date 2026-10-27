import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { consolidateDomains, outputPaths, readDomains, sourcePath } from "../scripts/workflow.mjs";
import { readReleases, readSourceDomains } from "../scripts/catalog.mjs";

const repo = "Example/containers";
const domains = {
  neuroimaging: { source_repositories: [repo] },
  microscopy: { source_repositories: [repo] },
};
const sha = "a".repeat(40);

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "catalog-workflow-"));
  await writeFile(join(root, "domains.json"), JSON.stringify({ domains }));
  const source = sourcePath(root, repo);
  await mkdir(join(source, "releases", "demo"), { recursive: true });
  await mkdir(join(source, "recipes", "demo"), { recursive: true });
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921", doi: "https://doi.org/10.123/demo" } },
    categories: ["visualization"],
  }));
  await writeFile(join(source, "recipes", "demo", "build.yaml"), "domains: [neuroimaging]\n");
  await mkdir(join(root, "neuroimaging"));
  await writeFile(join(root, "neuroimaging", "apps.json"), JSON.stringify({
    demo: { apps: { "demo 1.0": { doi: "https://doi.org/10.123/old", license: ["MIT"] } } },
  }));
  return { root, source };
}

test("validates source lists and generated paths", async () => {
  const { root } = await fixture();
  assert.deepEqual(await readDomains(root), domains);
  assert.deepEqual(outputPaths(domains), [
    "neuroimaging/apps.json", "neuroimaging/logs.txt", "neuroimaging/manifest.json",
    "microscopy/apps.json", "microscopy/logs.txt", "microscopy/manifest.json",
  ]);
  await writeFile(join(root, "domains.json"), JSON.stringify({ domains: {
    neuroimaging: { source_repositories: [repo, repo] },
  } }));
  await assert.rejects(readDomains(root), /Invalid source_repositories/);
});

test("domain move routes every version and carries established license", async () => {
  const { root, source } = await fixture();
  await consolidateDomains(root, domains, { doiDelayMs: 0, sourceCommits: { [repo]: sha } });
  let neuro = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.deepEqual(neuro.demo.apps["demo 1.0"].license, ["MIT"]);
  assert.equal(Object.hasOwn(neuro.demo.apps["demo 1.0"], "show_in_menu"), false);
  await writeFile(join(source, "recipes", "demo", "build.yaml"), "domains: [microscopy]\n");
  await consolidateDomains(root, domains, { doiDelayMs: 0, sourceCommits: { [repo]: sha } });
  neuro = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  const micro = JSON.parse(await readFile(join(root, "microscopy", "apps.json"), "utf8"));
  assert.deepEqual(neuro, {});
  assert.deepEqual(micro.demo.apps["demo 1.0"].license, ["MIT"]);
  assert.equal((await readFile(join(root, "neuroimaging", "logs.txt"), "utf8")), "");
  assert.equal((await readFile(join(root, "microscopy", "logs.txt"), "utf8")),
    "demo_1.0_20260921 categories:visualization,\n");
  assert.deepEqual(JSON.parse(await readFile(join(root, "microscopy", "manifest.json"), "utf8"))
    .source_commits, { [repo]: sha });
});

test("catalog generation can run before DOI publication", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921" } }, categories: ["visualization"],
  }));
  await writeFile(join(root, "neuroimaging", "apps.json"), "{}\n");

  await consolidateDomains(root, domains, {
    doiDelayMs: 0, sourceCommits: { [repo]: sha },
  });

  const catalog = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.equal(Object.hasOwn(catalog.demo.apps["demo 1.0"], "doi"), false);
});

test("a source missing from its declared domain fails", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "recipes", "demo", "build.yaml"), "domains: [microscopy]\n");
  await assert.rejects(readSourceDomains(source, await readReleases(source), {
    neuroimaging: { source_repositories: [repo] },
    microscopy: { source_repositories: ["Other/repo"] },
  }, repo), /does not list/);
});

test("two repositories publishing the same app fail with both source names", async () => {
  const { root } = await fixture();
  const second = "Other/containers";
  const source = sourcePath(root, second);
  await mkdir(join(source, "releases", "demo"), { recursive: true });
  await mkdir(join(source, "recipes", "demo"), { recursive: true });
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921" } }, categories: ["visualization"],
  }));
  await writeFile(join(source, "recipes", "demo", "build.yaml"), "domains: [neuroimaging]\n");
  const configured = { neuroimaging: { source_repositories: [repo, second] } };
  await assert.rejects(consolidateDomains(root, configured, {
    sourceCommits: { [repo]: sha, [second]: "b".repeat(40) }, doiDelayMs: 0,
  }), /Duplicate app.*Example\/containers.*Other\/containers/);
});

test("duplicate app identity across separate domains fails", async () => {
  const { root } = await fixture();
  const second = "Other/containers";
  const source = sourcePath(root, second);
  await mkdir(join(source, "releases", "demo"), { recursive: true });
  await mkdir(join(source, "recipes", "demo"), { recursive: true });
  await writeFile(join(source, "releases", "demo", "2.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20261010" } }, categories: ["visualization"],
  }));
  await writeFile(join(source, "recipes", "demo", "build.yaml"), "domains: [microscopy]\n");
  const configured = {
    neuroimaging: { source_repositories: [repo] },
    microscopy: { source_repositories: [second] },
  };
  await assert.rejects(consolidateDomains(root, configured, {
    sourceCommits: { [repo]: sha, [second]: "b".repeat(40) }, doiDelayMs: 0,
  }), /Duplicate app.*Example\/containers.*Other\/containers/);
});
