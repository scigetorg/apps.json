import { execFile } from "node:child_process";
import { readFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import { consolidate } from "./catalog.mjs";

const exec = promisify(execFile);
const DOMAIN_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const GENERATED = ["apps.json", "applist.json", "logs.txt", "manifest.json"];

export async function readDomains(root) {
  const document = JSON.parse(await readFile(join(root, "domains.json"), "utf8"));
  if (!document || typeof document !== "object" || Array.isArray(document) ||
      !document.domains || typeof document.domains !== "object" ||
      Array.isArray(document.domains) || !Object.keys(document.domains).length) {
    throw new Error("domains.json must contain a nonempty domains object");
  }
  const domains = {};
  for (const [id, config] of Object.entries(document.domains).sort(([a], [b]) => a.localeCompare(b))) {
    if (!DOMAIN_ID.test(id)) throw new Error(`Invalid domain ID: ${id}`);
    if (!config || typeof config !== "object" || Array.isArray(config) ||
        !REPOSITORY.test(config.source_repository ?? "")) {
      throw new Error(`Invalid source_repository for ${id}`);
    }
    if (config.dispatch_repository !== undefined && !REPOSITORY.test(config.dispatch_repository)) {
      throw new Error(`Invalid dispatch_repository for ${id}`);
    }
    domains[id] = config;
  }
  return domains;
}

export function outputPaths(domains) {
  return Object.keys(domains).flatMap((id) => GENERATED.map((name) => `${id}/${name}`));
}

export function changedDomains(paths, domains) {
  const affected = new Set();
  for (const path of paths) {
    const match = /^([^/]+)\/(apps|applist)\.json$/.exec(path);
    if (match && Object.hasOwn(domains, match[1])) affected.add(match[1]);
  }
  return [...affected].sort();
}

export function requiresDispatchToken(domains) {
  return Object.values(domains).some((config) => Boolean(config.dispatch_repository));
}

export async function checkoutSources(root, domains) {
  const sourceBase = join(root, ".catalog-sources");
  await mkdir(sourceBase, { recursive: true });
  await Promise.all(Object.entries(domains).map(async ([id, config]) => {
    const destination = join(sourceBase, id);
    await exec("git", ["clone", "--depth=1", `https://github.com/${config.source_repository}.git`, destination]);
    console.log(`Checked out ${config.source_repository} for ${id}`);
  }));
}

export async function consolidateDomains(root, domains, { doiDelayMs = 2100, sourceCommits = {} } = {}) {
  for (const [id, config] of Object.entries(domains)) {
    const sourceRoot = join(root, ".catalog-sources", id);
    const sourceCommit = sourceCommits[id] ??
      (await exec("git", ["-C", sourceRoot, "rev-parse", "HEAD"])).stdout.trim();
    if (!/^[0-9a-f]{40}$/.test(sourceCommit)) throw new Error(`Invalid source commit for ${id}`);
    const files = await consolidate({
      root: join(root, id), sourceRoot, sourceCommit, doiDelayMs,
    });
    console.log(`Generated ${id}: ${Object.keys(files).join(", ")} from ${config.source_repository}@${sourceCommit}`);
  }
}

export async function dispatchDomains(domains, affected, { token, repository, commit } = {}) {
  for (const id of affected) {
    const config = domains[id];
    if (!config) throw new Error(`Unknown domain to dispatch: ${id}`);
    if (!config.dispatch_repository) continue;
    if (!token || !repository || !/^[0-9a-f]{40}$/.test(commit ?? "")) {
      throw new Error("SITE_DISPATCH_TOKEN, GITHUB_REPOSITORY, and catalog commit are required for dispatch");
    }
    const response = await fetch(`https://api.github.com/repos/${config.dispatch_repository}/dispatches`, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "neurodesk-apps-catalog",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({
        event_type: "NeuroDesk/apps.json update",
        client_payload: {
          catalog_repo: repository,
          domain: id,
          apps_path: `${id}/apps.json`,
          applist_path: `${id}/applist.json`,
          commit,
          source_repository: config.source_repository,
        },
      }),
    });
    if (!response.ok) {
      throw new Error(`Website dispatch for ${id}: HTTP ${response.status} ${(await response.text()).slice(0, 300)}`);
    }
    console.log(`Dispatched ${id} to ${config.dispatch_repository}`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const root = process.cwd();
  const domains = await readDomains(root);
  const command = process.argv[2];
  if (command === "checkout") {
    await checkoutSources(root, domains);
  } else if (command === "check-token") {
    if (requiresDispatchToken(domains) && !process.env.SITE_DISPATCH_TOKEN) {
      throw new Error("SITE_DISPATCH_TOKEN is required for configured website dispatches");
    }
  } else if (command === "run") {
    await consolidateDomains(root, domains);
  } else if (command === "output-paths") {
    process.stdout.write(`${outputPaths(domains).join("\n")}\n`);
  } else if (command === "changed-domains") {
    let input = "";
    for await (const chunk of process.stdin) input += chunk;
    process.stdout.write(`${JSON.stringify(changedDomains(input.split(/\r?\n/).filter(Boolean), domains))}\n`);
  } else if (command === "dispatch") {
    const affected = process.env.FORCE_SITE_DISPATCH === "true"
      ? Object.keys(domains)
      : JSON.parse(process.env.CHANGED_DOMAINS ?? "[]");
    const { stdout } = await exec("git", ["rev-parse", "HEAD"], { cwd: root });
    await dispatchDomains(domains, affected, {
      token: process.env.SITE_DISPATCH_TOKEN,
      repository: process.env.GITHUB_REPOSITORY,
      commit: stdout.trim(),
    });
  } else {
    throw new Error(`Unknown command: ${command}`);
  }
}
