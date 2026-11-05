import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, unlink, writeFile } from "node:fs/promises";
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
const resolveLicenses = async (titles) => ({
  resolved: Object.fromEntries(Object.keys(titles).map((title) => [title, {
    license: ["MIT"], license_details: [],
  }])), errors: {},
});

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
    demo: { apps: { "demo 1.0": { image: "demo_1.0", version: "20260921",
      doi: "https://doi.org/10.123/old", license: ["MIT"] } } },
  }));
  return { root, source };
}

test("validates source lists and generated paths", async () => {
  const { root } = await fixture();
  assert.deepEqual(await readDomains(root), domains);
  assert.deepEqual(outputPaths(domains), [
    "neuroimaging/apps.json", "neuroimaging/manifest.json",
    "microscopy/apps.json", "microscopy/manifest.json",
  ]);
  await writeFile(join(root, "domains.json"), JSON.stringify({ domains: {
    neuroimaging: { source_repositories: [repo, repo] },
  } }));
  await assert.rejects(readDomains(root), /Invalid source_repositories/);
});

test("domain move routes every version and carries established license", async () => {
  const { root, source } = await fixture();
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses,
  });
  let neuro = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.deepEqual(neuro.demo.apps["demo 1.0"].license, ["MIT"]);
  assert.equal(neuro.demo.apps["demo 1.0"].doi, "https://doi.org/10.123/demo");
  assert.equal(Object.hasOwn(neuro.demo.apps["demo 1.0"], "show_in_menu"), false);
  await writeFile(join(source, "recipes", "demo", "build.yaml"), "domains: [microscopy]\n");
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses,
  });
  neuro = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  const micro = JSON.parse(await readFile(join(root, "microscopy", "apps.json"), "utf8"));
  assert.deepEqual(neuro, {});
  assert.deepEqual(micro.demo.apps["demo 1.0"].license, ["MIT"]);
  assert.equal(JSON.parse(await readFile(join(root, "neuroimaging", "manifest.json"), "utf8"))
    .entries, 0);
  assert.equal(JSON.parse(await readFile(join(root, "microscopy", "manifest.json"), "utf8"))
    .entries, 1);
  assert.deepEqual(JSON.parse(await readFile(join(root, "microscopy", "manifest.json"), "utf8"))
    .source_commits, { [repo]: sha });
  assert.equal(Object.hasOwn(JSON.parse(await readFile(join(root, "microscopy", "manifest.json"), "utf8")),
    "license_provenance"), false);
});

test("every run reads an already licensed image from its recipe history", async () => {
  const { root } = await fixture();
  const looked = [];
  const resolve = async (titles) => {
    looked.push(...Object.keys(titles));
    return { resolved: Object.fromEntries(Object.keys(titles).map((title) => [title, {
      license: ["Apache-2.0", "MIT"], license_details: [],
    }])), errors: {} };
  };
  await consolidateDomains(root, domains, { sourceCommits: { [repo]: sha }, resolveLicenses: resolve });
  assert.deepEqual(looked, ["demo_1.0_20260921"]);
  const neuro = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.deepEqual(neuro.demo.apps["demo 1.0"].license, ["Apache-2.0", "MIT"]);
});

test("deleting a release JSON removes that image and keeps another version", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "releases", "demo", "2.0.json"), JSON.stringify({
    apps: { "demo 2.0": { version: "20261010" } }, categories: ["visualization"],
  }));
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses, publishedDois: {},
  });
  await unlink(join(source, "releases", "demo", "1.0.json"));
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses, publishedDois: {},
  });
  const catalog = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.deepEqual(Object.keys(catalog.demo.apps), ["demo 2.0"]);
  assert.equal(catalog.demo.apps["demo 2.0"].image, "demo_2.0");
  const manifest = JSON.parse(await readFile(join(root, "neuroimaging", "manifest.json"), "utf8"));
  assert.equal(manifest.entries, 1);
  assert.equal(Object.hasOwn(manifest, "license_provenance"), false);
});

test("deleting the last release JSON publishes an empty catalog", async () => {
  const { root, source } = await fixture();
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses, publishedDois: {},
  });
  await unlink(join(source, "releases", "demo", "1.0.json"));
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses, publishedDois: {},
  });
  assert.deepEqual(JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8")), {});
  const manifest = JSON.parse(await readFile(join(root, "neuroimaging", "manifest.json"), "utf8"));
  assert.equal(manifest.entries, 0);
});

test("a corrected release license can replace the previous build's value", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921", license: ["Apache-2.0"] } },
    categories: ["visualization"],
  }));
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, publishedDois: {},
    resolveLicenses: async (titles) => ({ resolved: Object.fromEntries(Object.keys(titles).map(
      (title) => [title, { license: ["Apache-2.0"], license_details: [] }])), errors: {} }),
  });
  const catalog = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.deepEqual(catalog.demo.apps["demo 1.0"].license, ["Apache-2.0"]);
});

test("catalog generation can run before DOI publication", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921" } }, categories: ["visualization"],
  }));
  await writeFile(join(root, "neuroimaging", "apps.json"), "{}\n");

  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses,
  });

  const catalog = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.equal(Object.hasOwn(catalog.demo.apps["demo 1.0"], "doi"), false);
});

test("one account lookup attaches a published DOI across domains", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921" } }, categories: ["visualization"],
  }));
  await writeFile(join(root, "neuroimaging", "apps.json"), "{}\n");
  let lookups = 0;
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses,
    lookupDois: async () => { lookups++; return { "demo_1.0_20260921": "10.123/new" }; },
  });
  const catalog = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  assert.equal(catalog.demo.apps["demo 1.0"].doi, "https://doi.org/10.123/new");
  assert.equal(lookups, 1);
});

test("Zenodo lookup failure does not block catalog generation", async () => {
  const { root, source } = await fixture();
  await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
    apps: { "demo 1.0": { version: "20260921" } }, categories: ["visualization"],
  }));
  await writeFile(join(root, "neuroimaging", "apps.json"), "{}\n");
  await consolidateDomains(root, domains, {
    sourceCommits: { [repo]: sha }, resolveLicenses,
    lookupDois: async () => { throw new Error("Zenodo unavailable"); },
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
