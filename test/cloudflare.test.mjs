import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  catalogAssets,
  publishCatalogs,
  verifyPublishedCatalogs,
} from "../scripts/cloudflare.mjs";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "neurodesk-cloudflare-"));
  await writeFile(
    join(root, "domains.json"),
    JSON.stringify({
      domains: {
        neuroimaging: { source_repositories: ["NeuroDesk/neurocontainers"] },
        microscopy: { source_repositories: ["Example/microcontainers"] },
      },
    }),
  );
  for (const id of ["neuroimaging", "microscopy"]) {
    await mkdir(join(root, id));
    await writeFile(
      join(root, id, "apps.json"),
      JSON.stringify({ [id]: { apps: {} } }),
    );
  }
  return root;
}

function ok(result) {
  return {
    ok: true,
    status: 200,
    json: async () => ({ success: true, result }),
  };
}

test("publishes configured domain paths and requested asset batches", async () => {
  const root = await fixture();
  const assets = await catalogAssets(root);
  const hashes = [...assets.values()].map(({ hash }) => hash);
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (options.method === "GET") return ok({ id: "catalog" });
    if (url.endsWith("assets-upload-session"))
      return ok({ jwt: "upload-token", buckets: [hashes] });
    if (url.includes("assets/upload?")) return ok({ jwt: "completion-token" });
    return ok({});
  };
  const paths = await publishCatalogs(root, {
    accountId: "account",
    apiToken: "api-token",
    workerName: "catalog",
    fetchImpl,
  });
  assert.deepEqual(paths, ["/microscopy/apps.json", "/neuroimaging/apps.json"]);
  assert.equal(calls.length, 5);
  const manifest = JSON.parse(calls[1].options.body).manifest;
  for (const [path, asset] of assets) {
    assert.deepEqual(manifest[path], {
      hash: createHash("sha256")
        .update(asset.bytes.toString("base64") + "json")
        .digest("hex")
        .slice(0, 32),
      size: asset.bytes.length,
    });
  }
  assert.equal(calls[2].options.headers.Authorization, "Bearer upload-token");
  for (const hash of hashes) {
    const part = calls[2].options.body.get(hash);
    assert.equal(part.type, "application/json");
    assert.equal(
      await part.text(),
      [...assets.values()]
        .find((asset) => asset.hash === hash)
        .bytes.toString("base64"),
    );
  }
  assert.equal(calls[3].options.method, "PUT");
  assert.equal(calls[3].options.headers.Authorization, "Bearer api-token");
  const metadata = JSON.parse(
    await calls[3].options.body.get("metadata").text(),
  );
  assert.equal(calls[3].options.body.get("metadata").type, "application/json");
  assert.equal(metadata.assets.jwt, "completion-token");
  assert.match(
    metadata.assets.config._headers,
    /Access-Control-Allow-Origin: \*/,
  );
  assert.equal(calls[4].url.endsWith("/subdomain"), true);
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    enabled: true,
    previews_enabled: false,
  });
});

test("reuses completion token when assets are unchanged", async () => {
  const root = await fixture();
  const calls = [];
  await publishCatalogs(root, {
    accountId: "account",
    apiToken: "api-token",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      if (options.method === "GET") return ok({ id: "apps-json" });
      return ok(
        url.endsWith("assets-upload-session")
          ? { jwt: "existing-assets", buckets: [] }
          : {},
      );
    },
  });
  assert.equal(calls.length, 4);
  assert.equal(
    JSON.parse(await calls[2].options.body.get("metadata").text()).assets.jwt,
    "existing-assets",
  );
});

test("creates the Worker before the first asset upload", async () => {
  const root = await fixture();
  const calls = [];
  await publishCatalogs(root, {
    accountId: "account",
    apiToken: "api-token",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      if (options.method === "GET") {
        return {
          ok: false,
          status: 404,
          json: async () => ({ success: false, errors: [] }),
        };
      }
      return ok(
        url.endsWith("assets-upload-session")
          ? { jwt: "existing-assets", buckets: [] }
          : {},
      );
    },
  });
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[1].url.endsWith("/workers/workers"), true);
  assert.deepEqual(JSON.parse(calls[1].options.body), { name: "apps-json" });
  assert.equal(calls[2].url.endsWith("assets-upload-session"), true);
});

test("stops before deployment on failed upload or invalid catalog", async () => {
  const root = await fixture();
  const assets = await catalogAssets(root);
  const hash = [...assets.values()][0].hash;
  let calls = 0;
  await assert.rejects(
    publishCatalogs(root, {
      accountId: "account",
      apiToken: "api-token",
      fetchImpl: async (url) => {
        calls++;
        if (url.endsWith("/workers/apps-json")) return ok({ id: "apps-json" });
        if (url.endsWith("assets-upload-session"))
          return ok({ jwt: "upload-token", buckets: [[hash]] });
        return {
          ok: false,
          status: 403,
          json: async () => ({
            success: false,
            errors: [{ code: 10000, message: "Unauthorized" }],
          }),
        };
      },
    }),
    /Cloudflare POST .*assets\/upload.*403.*Unauthorized/,
  );
  assert.equal(calls, 3);
  await writeFile(join(root, "microscopy", "apps.json"), "[]");
  await assert.rejects(catalogAssets(root), /must contain a JSON object/);
});

test("verifies public bytes, content type, and CORS after deployment", async () => {
  const root = await fixture();
  const assets = await catalogAssets(root);
  const requests = [];
  const fetchImpl = async (url) => {
    requests.push(url);
    if (url.includes("api.cloudflare.com"))
      return ok({ subdomain: { url: "https://apps-json.example.workers.dev" } });
    const path = new URL(url).pathname;
    return new Response(assets.get(path).bytes, {
      headers: {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      },
    });
  };
  assert.equal(
    await verifyPublishedCatalogs(root, {
      accountId: "account",
      apiToken: "api-token",
      fetchImpl,
      delayMs: 0,
    }),
    "https://apps-json.example.workers.dev",
  );
  assert.equal(requests.length, 3);
  assert.equal(
    requests[0],
    "https://api.cloudflare.com/client/v4/accounts/account/workers/workers/apps-json",
  );
  await assert.rejects(
    verifyPublishedCatalogs(root, {
      accountId: "account",
      apiToken: "api-token",
      attempts: 1,
      fetchImpl: async (url) =>
        url.includes("api.cloudflare.com")
          ? ok({ subdomain: { url: "https://apps-json.example.workers.dev" } })
          : new Response("{}", {
              headers: { "content-type": "application/json" },
            }),
    }),
    /Could not verify .*content mismatch/,
  );
  await assert.rejects(
    verifyPublishedCatalogs(root, {
      accountId: "account",
      apiToken: "api-token",
      attempts: 1,
      fetchImpl: async (url) =>
        url.includes("api.cloudflare.com")
          ? ok({ subdomain: { url: "https://apps-json.example.workers.dev" } })
          : new Response(assets.get(new URL(url).pathname).bytes, {
              headers: { "content-type": "application/json" },
            }),
    }),
    /Could not verify .*access-control-allow-origin mismatch \(missing\)/,
  );
});
