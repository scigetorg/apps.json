import { execFile } from "node:child_process";
import { readFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import { consolidate } from "./catalog.mjs";

const exec = promisify(execFile);
const DOMAIN_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const GENERATED = ["apps.json", "logs.txt", "manifest.json"];

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
    if (Object.keys(config).some((key) => key !== "source_repository")) {
      throw new Error(`Unknown configuration field for ${id}`);
    }
    domains[id] = config;
  }
  return domains;
}

export function outputPaths(domains) {
  return Object.keys(domains).flatMap((id) => GENERATED.map((name) => `${id}/${name}`));
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const root = process.cwd();
  const domains = await readDomains(root);
  const command = process.argv[2];
  if (command === "checkout") {
    await checkoutSources(root, domains);
  } else if (command === "run") {
    await consolidateDomains(root, domains);
  } else if (command === "output-paths") {
    process.stdout.write(`${outputPaths(domains).join("\n")}\n`);
  } else {
    throw new Error(`Unknown command: ${command}`);
  }
}
