import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  changedDomains,
  consolidateDomains,
  dispatchDomains,
  outputPaths,
  readDomains,
  requiresDispatchToken,
} from "../scripts/workflow.mjs";

const configs = {
  neuroimaging: {
    source_repository: "NeuroDesk/neurocontainers",
    dispatch_repository: "NeuroDesk/neurodesk.github.io",
  },
  microscopy: { source_repository: "Example/microcontainers" },
};

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "neurodesk-domains-"));
  await writeFile(join(root, "domains.json"), JSON.stringify({ domains: configs }));
  for (const [id, build] of [["neuroimaging", "20260921"], ["microscopy", "20260922"]]) {
    const source = join(root, ".catalog-sources", id);
    await mkdir(join(source, "releases", "demo"), { recursive: true });
    await writeFile(join(source, "releases", "demo", "1.0.json"), JSON.stringify({
      apps: { "demo 1.0": { version: build, doi: `https://doi.org/10.123/${id}` } },
      categories: [id],
    }));
  }
  await mkdir(join(root, "neuroimaging"));
  await writeFile(join(root, "neuroimaging", "apps.json"), JSON.stringify({
    demo: { apps: { "demo 1.0": { show_in_applist: false } } },
  }));
  return root;
}

test("validates domain IDs and repositories", async () => {
  const root = await fixture();
  assert.deepEqual(await readDomains(root), configs);
  await writeFile(join(root, "domains.json"), JSON.stringify({
    domains: { "../escape": { source_repository: "Example/repo" } },
  }));
  await assert.rejects(readDomains(root), /Invalid domain ID/);
});

test("generates isolated catalogs for two configured domains", async () => {
  const root = await fixture();
  const domains = await readDomains(root);
  await consolidateDomains(root, domains, {
    doiDelayMs: 0,
    sourceCommits: { neuroimaging: "a".repeat(40), microscopy: "b".repeat(40) },
  });
  const neuro = JSON.parse(await readFile(join(root, "neuroimaging", "apps.json"), "utf8"));
  const micro = JSON.parse(await readFile(join(root, "microscopy", "apps.json"), "utf8"));
  assert.equal(neuro.demo.apps["demo 1.0"].show_in_applist, false);
  assert.equal(micro.demo.apps["demo 1.0"].show_in_applist, true);
  assert.equal(neuro.demo.apps["demo 1.0"].version, "20260921");
  assert.equal(micro.demo.apps["demo 1.0"].version, "20260922");
  assert.equal((await readFile(join(root, "microscopy", "logs.txt"), "utf8")),
    "demo_1.0_20260922 categories:microscopy,\n");
  assert.equal(JSON.parse(await readFile(join(root, "microscopy", "manifest.json"), "utf8")).source_commit.length, 40);
  assert.equal(outputPaths(domains).length, 8);
  assert.equal(requiresDispatchToken(domains), true);
  assert.equal(requiresDispatchToken({ microscopy: domains.microscopy }), false);
  assert.deepEqual(changedDomains([
    "neuroimaging/apps.json", "microscopy/applist.json", "microscopy/manifest.json", "other/apps.json",
  ], domains), ["microscopy", "neuroimaging"]);
});

test("dispatches only a changed domain with its path and source repository", async () => {
  const previousFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return { ok: true };
  };
  try {
    await dispatchDomains(configs, ["neuroimaging", "microscopy"], {
      token: "test-token", repository: "NeuroDesk/apps.json", commit: "a".repeat(40),
    });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].body.client_payload.domain, "neuroimaging");
    assert.equal(requests[0].body.client_payload.applist_path, "neuroimaging/applist.json");
    assert.equal(requests[0].body.client_payload.source_repository, "NeuroDesk/neurocontainers");
  } finally {
    globalThis.fetch = previousFetch;
  }
});
