import YAML from "yaml";
import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";

const IMAGE = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function jsonText(value) {
  return `${JSON.stringify(value, null, 4)}\n`;
}

export async function readPreviousCatalog(root) {
  const path = join(root, "apps.json");
  let text;
  try {
    text = await readFile(path, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return { catalog: {} };
    throw error;
  }
  const catalog = JSON.parse(text);
  if (!catalog || typeof catalog !== "object" || Array.isArray(catalog)) {
    throw new Error("Published apps.json must contain an object");
  }
  return { catalog };
}

export async function readReleases(sourceRoot) {
  const releaseRoot = join(sourceRoot, "releases");
  const tools = (await readdir(releaseRoot, { withFileTypes: true }))
    .filter((item) => item.isDirectory()).map((item) => item.name).sort();
  const releases = [];
  for (const tool of tools) {
    const directory = join(releaseRoot, tool);
    for (const item of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      if (!item.isFile() || !item.name.endsWith(".json")) continue;
      const path = `releases/${tool}/${item.name}`;
      releases.push({ path, data: JSON.parse(await readFile(join(sourceRoot, path), "utf8")) });
    }
  }
  if (!releases.length) throw new Error(`No release JSON files found in ${sourceRoot}`);
  return releases;
}

function imageKey(tool, release, app) {
  const image = app.image ?? `${tool}_${release}`;
  if (typeof image !== "string" || !IMAGE.test(image)) throw new Error(`Invalid image: ${image}`);
  return image;
}

function validBuildDate(value) {
  if (typeof value !== "string" || !/^\d{8}$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(4, 6));
  const day = Number(value.slice(6, 8));
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() + 1 === month && date.getUTCDate() === day;
}

export function buildCatalog(releases, previous) {
  if (!releases.length) throw new Error("No releases to consolidate");
  const catalog = {};
  for (const { path, data } of [...releases].sort((a, b) => a.path.localeCompare(b.path))) {
    const match = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    if (!match || !data || typeof data !== "object" || Array.isArray(data)) throw new Error(`Invalid release ${path}`);
    const [, tool, release] = match;
    if (!data.apps || typeof data.apps !== "object" || Array.isArray(data.apps) || !Object.keys(data.apps).length || !Array.isArray(data.categories)) {
      throw new Error(`Invalid apps or categories in ${path}`);
    }
    const entry = catalog[tool] ??= { apps: {}, categories: [] };
    for (const category of data.categories) {
      if (typeof category !== "string" || !category) throw new Error(`Invalid category in ${path}`);
      if (!entry.categories.includes(category)) entry.categories.push(category);
    }
    const old = previous.catalog[tool] ?? {};
    for (const [name, app] of Object.entries(data.apps)) {
      if (!app || typeof app !== "object" || Array.isArray(app) || !validBuildDate(app.version)) {
        throw new Error(`Invalid app ${name} in ${path}`);
      }
      imageKey(tool, release, app);
      const prior = old.apps?.[name] ?? {};
      const combined = { ...app };
      for (const flag of ["show_in_menu", "show_in_applist"]) {
        const value = app[flag] ?? prior[flag] ?? old[flag] ?? true;
        if (typeof value !== "boolean") throw new Error(`${flag} must be boolean for ${name}`);
        combined[flag] = value;
      }
      for (const field of ["doi", "license"]) {
        if (combined[field] === undefined && prior[field] !== undefined) combined[field] = prior[field];
      }
      if (entry.apps[name] && JSON.stringify(entry.apps[name]) !== JSON.stringify(combined)) {
        throw new Error(`Conflicting app ${name} in ${path}`);
      }
      entry.apps[name] = combined;
    }
  }
  return Object.fromEntries(Object.keys(catalog).sort().map((tool) => [tool, catalog[tool]]));
}

export async function makeFiles(catalog, sourceCommit, releases) {
  const rows = new Map();
  const visible = [];
  for (const { path, data } of releases) {
    const [, tool, release] = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    for (const app of Object.values(data.apps)) {
      const image = imageKey(tool, release, app);
      const application = `${image}_${app.version}`;
      rows.set(application, `${application} categories:${catalog[tool].categories.join(",")},`);
    }
    const primary = catalog[tool].apps[`${tool} ${release}`];
    if (primary?.show_in_applist) {
      const application = `${imageKey(tool, release, primary)}_${primary.version}`;
      visible.push({ application, categories: catalog[tool].categories });
    }
  }
  if (!rows.size) throw new Error("Catalog has no container images");
  const logs = `${[...rows].sort(([a], [b]) => a.localeCompare(b)).map(([, row]) => row).join("\n")}\n`;
  const sha256 = createHash("sha256").update(logs).digest("hex");
  return {
    "apps.json": jsonText(catalog),
    "logs.txt": logs,
    "manifest.json": jsonText({ sha256, entries: rows.size, source_commit: sourceCommit }),
    "applist.json": jsonText({ list: visible.sort((a, b) => a.application.localeCompare(b.application)) }),
  };
}

async function zenodoRecords(title) {
  const query = new URLSearchParams({ q: `metadata.title:"${title}"`, size: "10" });
  for (let attempt = 0; attempt < 4; attempt++) {
    const response = await fetch(`https://zenodo.org/api/records?${query}`, { headers: { Accept: "application/json" } });
    if (response.ok) return (await response.json()).hits?.hits ?? [];
    if (![429, 500, 502, 503, 504].includes(response.status) || attempt === 3) {
      throw new Error(`Zenodo DOI lookup for ${title}: HTTP ${response.status}`);
    }
    await sleep(5000 * 2 ** attempt);
  }
  return [];
}

export async function collectDoiMetadata(catalog, releases, delayMs = 2100) {
  const found = {};
  const groups = new Map();
  for (const { path, data } of releases) {
    const [, tool, release] = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    for (const [name, app] of Object.entries(data.apps)) {
      const title = `${imageKey(tool, release, app)}_${app.version}`;
      const key = `${tool}\0${title}`;
      if (!groups.has(key)) groups.set(key, { tool, title, names: new Set() });
      groups.get(key).names.add(name);
    }
  }
  for (const { tool, title, names } of groups.values()) {
    const related = [...names].map((name) => [name, catalog[tool].apps[name]]);
    const existing = related.find(([, app]) => app.doi)?.[1].doi;
    if (existing) {
      for (const [name, app] of related) if (!app.doi) (found[tool] ??= {})[name] = existing;
      continue;
    }
    const records = await zenodoRecords(title);
    const matches = records.filter((record) => record.metadata?.title === title);
    matches.sort((a, b) => Number(b.id) - Number(a.id));
    const doi = matches[0]?.doi ?? matches[0]?.metadata?.doi;
    if (doi) {
      for (const [name, item] of related) if (!item.doi) (found[tool] ??= {})[name] = `https://doi.org/${doi}`;
    }
    if (delayMs) await sleep(delayMs);
  }
  return found;
}

export async function collectLicenseMetadata(catalog, sourceRoot) {
  const found = {};
  await Promise.all(Object.entries(catalog).map(async ([tool, entry]) => {
    const recipeTool = tool.replace(/_arm64$/, "");
    const path = `recipes/${recipeTool}/build.yaml`;
    let yaml;
    try {
      yaml = await readFile(join(sourceRoot, path), "utf8");
    } catch (error) {
      if (error.code === "ENOENT") return;
      throw error;
    }
    const document = YAML.parse(yaml);
    const copyrights = Array.isArray(document?.copyright) ? document.copyright : [];
    const licenses = [...new Set(copyrights.map((item) => item?.license).filter((item) => typeof item === "string" && item))].sort();
    if (!licenses.length) return;
    for (const [name, app] of Object.entries(entry.apps)) {
      if (!app.license) (found[tool] ??= {})[name] = licenses;
    }
  }));
  return found;
}

export function applyMetadata(catalog, dois, licenses) {
  const updated = structuredClone(catalog);
  for (const [tool, entry] of Object.entries(updated)) {
    for (const [name, app] of Object.entries(entry.apps)) {
      if (dois[tool]?.[name]) app.doi = dois[tool][name];
      if (licenses[tool]?.[name]) app.license = licenses[tool][name];
    }
  }
  return updated;
}

export async function consolidate({ root, sourceRoot, sourceCommit, doiDelayMs = 2100 }) {
  const [previous, releases] = await Promise.all([
    readPreviousCatalog(root), readReleases(sourceRoot),
  ]);
  const base = buildCatalog(releases, previous);
  const [dois, licenses] = await Promise.all([
    collectDoiMetadata(base, releases, doiDelayMs),
    collectLicenseMetadata(base, sourceRoot),
  ]);
  const files = await makeFiles(applyMetadata(base, dois, licenses), sourceCommit, releases);
  await mkdir(root, { recursive: true });
  for (const [name, content] of Object.entries(files)) {
    const destination = join(root, name);
    const temporary = `${destination}.tmp`;
    await writeFile(temporary, content);
    await rename(temporary, destination);
  }
  return files;
}
