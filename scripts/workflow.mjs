import { execFile } from "node:child_process";
import { readFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import { consolidate, imageBuild, readReleases, readSourceDomains, readPreviousCatalog } from "./catalog.mjs";

const exec = promisify(execFile);
const DOMAIN_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const GENERATED = ["apps.json", "manifest.json"];
export const sourcePath = (root, repository) =>
  join(root, ".catalog-sources", encodeURIComponent(repository));

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
        !Array.isArray(config.source_repositories) || !config.source_repositories.length ||
        config.source_repositories.some((repository) => !REPOSITORY.test(repository)) ||
        new Set(config.source_repositories).size !== config.source_repositories.length) {
      throw new Error(`Invalid source_repositories for ${id}`);
    }
    if (Object.keys(config).some((key) => key !== "source_repositories")) {
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
  const repositories = new Set(Object.values(domains).flatMap((config) => config.source_repositories));
  await Promise.all([...repositories].map(async (repository) => {
    const destination = sourcePath(root, repository);
    await exec("git", ["clone", "--depth=1", `https://github.com/${repository}.git`, destination]);
    console.log(`Checked out ${repository}`);
  }));
}

export function assertUniqueSources(sources) {
  const apps = new Map();
  const images = new Map();
  for (const [repository, source] of sources) {
    for (const { path, data } of source.releases) {
      const [, tool, release] = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
      for (const [name, app] of Object.entries(data.apps)) {
        const appKey = `${tool}\0${name}`;
        const image = imageBuild(tool, release, app);
        for (const [key, owners, kind] of [[appKey, apps, "app"], [image, images, "image"]]) {
          const owner = owners.get(key);
          if (owner && owner !== repository)
            throw new Error(`Duplicate ${kind} ${kind === "app" ? `${tool}/${name}` : image} from ${owner} and ${repository}`);
          owners.set(key, repository);
        }
      }
    }
  }
}

export async function consolidateDomains(root, domains, { doiDelayMs = 2100, sourceCommits = {} } = {}) {
  const repositories = [...new Set(Object.values(domains).flatMap((config) => config.source_repositories))];
  const sources = new Map();
  for (const repository of repositories) {
    const sourceRoot = sourcePath(root, repository);
    const sourceCommit = sourceCommits[repository] ??
      (await exec("git", ["-C", sourceRoot, "rev-parse", "HEAD"])).stdout.trim();
    if (!/^[0-9a-f]{40}$/.test(sourceCommit)) throw new Error(`Invalid source commit for ${repository}`);
    const releases = await readReleases(sourceRoot);
    const metadata = await readSourceDomains(sourceRoot, releases, domains, repository);
    sources.set(repository, { sourceRoot, sourceCommit, releases, metadata });
  }
  assertUniqueSources(sources);
  const previousByDomain = {};
  for (const id of Object.keys(domains))
    previousByDomain[id] = await readPreviousCatalog(join(root, id));
  const previousByIdentity = new Map();
  for (const [id, previous] of Object.entries(previousByDomain)) {
    const priorSources = Object.keys(previous.manifest?.source_commits ?? {});
    const singleSource = priorSources.length === 1 ? priorSources[0] :
      domains[id].source_repositories.length === 1 ? domains[id].source_repositories[0] : null;
    for (const [tool, entry] of Object.entries(previous.catalog)) {
      for (const [name, app] of Object.entries(entry.apps ?? {})) {
        const repository = previous.manifest?.app_sources?.[tool]?.[name] ??
          previous.manifest?.tool_sources?.[tool] ?? singleSource;
        if (!repository) continue;
        const key = JSON.stringify([repository, tool, name]);
        if (!previousByIdentity.has(key)) previousByIdentity.set(key, app);
      }
    }
  }
  for (const [id, config] of Object.entries(domains)) {
    const selected = config.source_repositories.map((repository) => ({ repository, ...sources.get(repository) }));
    const files = await consolidate({
      root: join(root, id), domain: id, sources: selected,
      previous: previousByDomain[id], previousByIdentity, doiDelayMs,
    });
    console.log(`Generated ${id}: ${Object.keys(files).join(", ")} from ${config.source_repositories.join(", ")}`);
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
  } else if (command === "source-repository") {
    throw new Error("source-repository is retired; use source_repositories in domains.json");
  } else {
    throw new Error(`Unknown command: ${command}`);
  }
}
