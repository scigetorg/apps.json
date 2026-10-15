import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { pathToFileURL } from "node:url";
import { readDomains } from "./workflow.mjs";

const API = "https://api.cloudflare.com/client/v4";
const WORKER_MODULE = "main.mjs";
const WORKER_SOURCE =
  'export default { fetch() { return new Response("Not found", { status: 404 }); } };\n';
const CORS_HEADERS = "/*\n  Access-Control-Allow-Origin: *";

function assetHash(bytes, path) {
  return createHash("sha256")
    .update(bytes.toString("base64") + extname(path).slice(1))
    .digest("hex")
    .slice(0, 32);
}

export async function catalogAssets(root) {
  const domains = await readDomains(root);
  const assets = new Map();
  for (const id of Object.keys(domains)) {
    const path = `/${id}/apps.json`;
    const bytes = await readFile(join(root, id, "apps.json"));
    const catalog = JSON.parse(bytes.toString("utf8"));
    if (!catalog || typeof catalog !== "object" || Array.isArray(catalog)) {
      throw new Error(`${path} must contain a JSON object`);
    }
    assets.set(path, { bytes, hash: assetHash(bytes, path) });
  }
  return assets;
}

async function apiRequest(
  fetchImpl,
  path,
  { method = "POST", token, body, json, allowNotFound = false } = {},
) {
  const response = await fetchImpl(`${API}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(json ? { "Content-Type": "application/json" } : {}),
    },
    body: json ? JSON.stringify(json) : body,
  });
  const payload = await response.json();
  if (allowNotFound && response.status === 404) return null;
  if (!response.ok || !payload.success) {
    const errors = payload.errors
      ?.map(({ code, message }) => `${code}: ${message}`)
      .join("; ");
    throw new Error(
      `Cloudflare ${method} ${path} failed (${response.status}): ${errors || "unknown error"}`,
    );
  }
  return payload.result;
}

export async function publishCatalogs(
  root,
  { accountId, apiToken, workerName = "apps-json", fetchImpl = fetch },
) {
  if (!accountId || !apiToken)
    throw new Error(
      "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required",
    );
  if (!/^[a-z0-9-]+$/.test(workerName)) throw new Error("Invalid Worker name");
  const assets = await catalogAssets(root);
  const manifest = Object.fromEntries(
    [...assets].map(([path, { hash, bytes }]) => [
      path,
      { hash, size: bytes.length },
    ]),
  );
  const accountPath = `/accounts/${encodeURIComponent(accountId)}/workers`;
  const workerPath = `${accountPath}/workers/${workerName}`;
  const worker = await apiRequest(fetchImpl, workerPath, {
    method: "GET",
    token: apiToken,
    allowNotFound: true,
  });
  if (!worker) {
    await apiRequest(fetchImpl, `${accountPath}/workers`, {
      token: apiToken,
      json: { name: workerName },
    });
  }
  const scriptPath = `${accountPath}/scripts/${workerName}`;
  const session = await apiRequest(
    fetchImpl,
    `${scriptPath}/assets-upload-session`,
    {
      token: apiToken,
      json: { manifest },
    },
  );
  if (!session?.jwt || !Array.isArray(session.buckets))
    throw new Error("Invalid Cloudflare asset upload session");

  const byHash = new Map(
    [...assets].map(([path, asset]) => [asset.hash, asset]),
  );
  let completionToken = session.jwt;
  for (const bucket of session.buckets) {
    if (!Array.isArray(bucket) || !bucket.length)
      throw new Error("Invalid Cloudflare asset upload bucket");
    const form = new FormData();
    for (const hash of bucket) {
      const asset = byHash.get(hash);
      if (!asset)
        throw new Error(`Cloudflare requested unknown asset hash: ${hash}`);
      form.append(
        hash,
        new Blob([asset.bytes.toString("base64")], {
          type: "application/json",
        }),
        hash,
      );
    }
    const uploaded = await apiRequest(
      fetchImpl,
      `/accounts/${encodeURIComponent(accountId)}/workers/assets/upload?base64=true`,
      {
        token: session.jwt,
        body: form,
      },
    );
    if (uploaded?.jwt) completionToken = uploaded.jwt;
  }
  if (session.buckets.length && completionToken === session.jwt) {
    throw new Error("Cloudflare did not return an asset completion token");
  }

  const metadata = {
    main_module: WORKER_MODULE,
    compatibility_date: "2026-10-15",
    assets: { jwt: completionToken, config: { headers: CORS_HEADERS } },
  };
  const form = new FormData();
  form.append(
    "metadata",
    new Blob([JSON.stringify(metadata)], { type: "application/json" }),
    "metadata.json",
  );
  form.append(
    WORKER_MODULE,
    new Blob([WORKER_SOURCE], { type: "application/javascript+module" }),
    WORKER_MODULE,
  );
  await apiRequest(fetchImpl, scriptPath, {
    method: "PUT",
    token: apiToken,
    body: form,
  });
  await apiRequest(fetchImpl, `${scriptPath}/subdomain`, {
    token: apiToken,
    json: { enabled: true, previews_enabled: false },
  });
  return [...assets.keys()];
}

export async function verifyPublishedCatalogs(
  root,
  {
    accountId,
    apiToken,
    workerName = "apps-json",
    fetchImpl = fetch,
    attempts = 5,
    delayMs = 1000,
  },
) {
  const accountPath = `/accounts/${encodeURIComponent(accountId)}/workers`;
  const account = await apiRequest(fetchImpl, `${accountPath}/subdomain`, {
    method: "GET",
    token: apiToken,
  });
  if (!account?.subdomain)
    throw new Error("Cloudflare account has no workers.dev subdomain");
  const baseUrl = `https://${workerName}.${account.subdomain}.workers.dev`;
  for (const [path, asset] of await catalogAssets(root)) {
    let failure;
    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        const response = await fetchImpl(`${baseUrl}${path}`);
        const bytes = Buffer.from(await response.arrayBuffer());
        if (
          !response.ok ||
          !bytes.equals(asset.bytes) ||
          !response.headers
            .get("content-type")
            ?.startsWith("application/json") ||
          response.headers.get("access-control-allow-origin") !== "*"
        ) {
          throw new Error(
            `status ${response.status}, content or headers mismatch`,
          );
        }
        failure = null;
        break;
      } catch (error) {
        failure = error;
        if (attempt + 1 < attempts)
          await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
    }
    if (failure)
      throw new Error(`Could not verify ${baseUrl}${path}: ${failure.message}`);
  }
  return baseUrl;
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  const options = {
    accountId: process.env.CLOUDFLARE_ACCOUNT_ID,
    apiToken: process.env.CLOUDFLARE_API_TOKEN,
    workerName: process.env.CLOUDFLARE_WORKER_NAME || "apps-json",
  };
  const paths = await publishCatalogs(process.cwd(), options);
  const baseUrl = await verifyPublishedCatalogs(process.cwd(), options);
  console.log(`Published and verified ${paths.join(", ")} at ${baseUrl}`);
}
