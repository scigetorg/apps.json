import YAML from "yaml";
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
    if (error.code === "ENOENT") return { catalog: {}, manifest: null };
    throw error;
  }
  const catalog = JSON.parse(text);
  if (!catalog || typeof catalog !== "object" || Array.isArray(catalog)) {
    throw new Error("Published apps.json must contain an object");
  }
  let manifest = null;
  try {
    manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  return { catalog, manifest };
}

export async function readReleases(sourceRoot) {
  const releaseRoot = join(sourceRoot, "releases");
  const tools = (await readdir(releaseRoot, { withFileTypes: true }))
    .filter((item) => item.isDirectory())
    .map((item) => item.name)
    .sort();
  const releases = [];
  for (const tool of tools) {
    const directory = join(releaseRoot, tool);
    for (const item of (await readdir(directory, { withFileTypes: true })).sort(
      (a, b) => a.name.localeCompare(b.name),
    )) {
      if (!item.isFile() || !item.name.endsWith(".json")) continue;
      const path = `releases/${tool}/${item.name}`;
      releases.push({
        path,
        data: JSON.parse(await readFile(join(sourceRoot, path), "utf8")),
      });
    }
  }
  if (!releases.length)
    throw new Error(`No release JSON files found in ${sourceRoot}`);
  return releases;
}

function imageKey(tool, release, app) {
  const image = app.image ?? `${tool}_${release}`;
  if (typeof image !== "string" || !IMAGE.test(image))
    throw new Error(`Invalid image: ${image}`);
  return image;
}

export function imageBuild(tool, release, app) {
  return `${imageKey(tool, release, app)}_${app.version}`;
}

export async function readSourceDomains(sourceRoot, releases, domains, repository) {
  const result = new Map();
  let retirements = [];
  try {
    const raw = await readFile(join(sourceRoot, "workflows/retired_recipes.yaml"), "utf8");
    retirements = YAML.parse(raw)?.retired ?? [];
    if (!Array.isArray(retirements)) throw new Error("Invalid retirement manifest");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const retired = new Map(retirements.map((entry) => [entry.recipe, entry]));
  for (const { path, data } of releases) {
    const tool = path.split("/")[1];
    if (result.has(tool)) continue;
    let recipe = data.recipe ?? tool;
    let document;
    try {
      document = YAML.parse(await readFile(join(sourceRoot, "recipes", recipe, "build.yaml"), "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      if (recipe === tool && tool.endsWith("_arm64")) {
        recipe = tool.slice(0, -6);
        try {
          document = YAML.parse(await readFile(join(sourceRoot, "recipes", recipe, "build.yaml"), "utf8"));
        } catch (inner) {
          if (inner.code !== "ENOENT") throw inner;
        }
      }
    }
    document ??= retired.get(recipe);
    const labels = document?.domains;
    if (!Array.isArray(labels) || !labels.length ||
        labels.some((label) => typeof label !== "string") ||
        new Set(labels).size !== labels.length) {
      throw new Error(`Missing or invalid domains for ${repository}/${tool} (${recipe})`);
    }
    for (const label of labels) {
      if (!Object.hasOwn(domains, label))
        throw new Error(`Unknown domain ${label} for ${repository}/${tool}`);
      if (!domains[label].source_repositories.includes(repository))
        throw new Error(`${repository}/${tool} declares ${label}, but that domain does not list ${repository}`);
    }
    result.set(tool, { domains: labels, recipe });
  }
  return result;
}

function validBuildDate(value) {
  if (typeof value !== "string" || !/^\d{8}$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(4, 6));
  const day = Number(value.slice(6, 8));
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() + 1 === month &&
    date.getUTCDate() === day
  );
}

export function buildCatalog(releases, previous, previousByIdentity = new Map()) {
  const catalog = {};
  const owners = new Map();
  for (const { path, data } of [...releases].sort((a, b) =>
    a.path.localeCompare(b.path),
  )) {
    const match = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    if (!match || !data || typeof data !== "object" || Array.isArray(data))
      throw new Error(`Invalid release ${path}`);
    const [, tool, release] = match;
    if (
      !data.apps ||
      typeof data.apps !== "object" ||
      Array.isArray(data.apps) ||
      !Object.keys(data.apps).length ||
      !Array.isArray(data.categories)
    ) {
      throw new Error(`Invalid apps or categories in ${path}`);
    }
    const entry = (catalog[tool] ??= { apps: {}, categories: [] });
    for (const category of data.categories) {
      if (typeof category !== "string" || !category)
        throw new Error(`Invalid category in ${path}`);
      if (!entry.categories.includes(category)) entry.categories.push(category);
    }
    for (const [name, app] of Object.entries(data.apps)) {
      if (
        !app ||
        typeof app !== "object" ||
        Array.isArray(app) ||
        !validBuildDate(app.version)
      ) {
        throw new Error(`Invalid app ${name} in ${path}`);
      }
      const key = JSON.stringify([data.repository, tool, name]);
      const prior = previousByIdentity.get(key) ??
        (!data.repository ? previous.catalog[tool]?.apps?.[name] : undefined) ?? {};
      // Every launcher carries its exact SIF image, including GUI-only releases.
      const combined = { ...app, image: imageKey(tool, release, app) };
      for (const flag of ["show_in_menu", "show_in_applist"]) {
        const value = app[flag] ?? data[flag];
        if (value !== undefined && typeof value !== "boolean")
          throw new Error(`${flag} must be boolean for ${name}`);
        if (value !== undefined) combined[flag] = value;
      }
      for (const field of ["doi", "license"]) {
        if (combined[field] === undefined && prior[field] !== undefined)
          combined[field] = prior[field];
      }
      const owner = owners.get(`${tool}\0${name}`);
      if (owner && owner !== data.repository)
        throw new Error(`Duplicate app ${tool}/${name} from ${owner} and ${data.repository}`);
      owners.set(`${tool}\0${name}`, data.repository);
      if (entry.apps[name] && JSON.stringify(entry.apps[name]) !== JSON.stringify(combined))
        throw new Error(`Conflicting app ${name} in ${path}`);
      entry.apps[name] = combined;
    }
  }
  return Object.fromEntries(
    Object.keys(catalog)
      .sort()
      .map((tool) => [tool, catalog[tool]]),
  );
}

export async function makeFiles(catalog, sourceCommits, releases) {
  const imageOwners = new Map();
  for (const { path, data } of releases) {
    const [, tool, release] = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    for (const app of Object.values(data.apps)) {
      const application = imageBuild(tool, release, app);
      const priorOwner = imageOwners.get(application);
      if (priorOwner && priorOwner !== data.repository)
        throw new Error(`Duplicate image ${application} from ${priorOwner} and ${data.repository}`);
      imageOwners.set(application, data.repository);
    }
  }
  const appSources = {};
  for (const { path, data } of releases) {
    const tool = path.split("/")[1];
    if (data.repository) {
      appSources[tool] ??= {};
      for (const name of Object.keys(data.apps)) appSources[tool][name] = data.repository;
    }
  }
  return {
    "apps.json": jsonText(catalog),
    "manifest.json": jsonText({
      entries: imageOwners.size,
      source_commits: sourceCommits,
      app_sources: appSources,
    }),
  };
}

async function zenodoRecords(title, token) {
  if (!token)
    throw new Error("ZENODO_TOKEN is required for Zenodo DOI lookups");
  const query = new URLSearchParams({
    q: `metadata.title:"${title}"`,
    size: "10",
  });
  for (let attempt = 0; attempt < 4; attempt++) {
    const response = await fetch(
      `https://zenodo.org/api/records?${query}`,
      {
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
        },
      },
    );
    if (response.ok) return (await response.json()).hits?.hits ?? [];
    if (response.status === 401 || response.status === 403) {
      throw new Error(
        `Zenodo DOI lookup for ${title}: HTTP ${response.status}; check ZENODO_TOKEN`,
      );
    }
    if (![429, 500, 502, 503, 504].includes(response.status) || attempt === 3) {
      throw new Error(
        `Zenodo DOI lookup for ${title}: HTTP ${response.status}`,
      );
    }
    await sleep(5000 * 2 ** attempt);
  }
  return [];
}

export async function collectDoiMetadata(
  catalog,
  releases,
  { delayMs = 2100, token } = {},
) {
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
      for (const [name, app] of related)
        if (!app.doi) (found[tool] ??= {})[name] = existing;
      continue;
    }
    const records = await zenodoRecords(title, token);
    const matches = records.filter(
      (record) => record.metadata?.title === title,
    );
    matches.sort((a, b) => Number(b.id) - Number(a.id));
    const doi = matches[0]?.doi ?? matches[0]?.metadata?.doi;
    if (doi) {
      for (const [name, item] of related)
        if (!item.doi) (found[tool] ??= {})[name] = `https://doi.org/${doi}`;
    }
    if (delayMs) await sleep(delayMs);
  }
  return found;
}

export async function collectLicenseMetadata(catalog, sourceRoots) {
  const found = {};
  await Promise.all(
    Object.entries(catalog).map(async ([tool, entry]) => {
      for (const [name, app] of Object.entries(entry.apps)) {
        if (app.license) continue;
        const source = typeof sourceRoots === "string" ?
          { root: sourceRoots, recipe: tool.replace(/_arm64$/, "") } : sourceRoots[tool]?.[name];
        if (!source) continue;
        let yaml;
        try {
          yaml = await readFile(join(source.root, "recipes", source.recipe, "build.yaml"), "utf8");
        } catch (error) {
          if (error.code === "ENOENT") continue;
          throw error;
        }
        const document = YAML.parse(yaml);
        const licenses = [...new Set((document?.copyright ?? [])
          .map((item) => item?.license)
          .filter((item) => typeof item === "string" && item))].sort();
        if (licenses.length) (found[tool] ??= {})[name] = licenses;
      }
    }),
  );
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

export async function consolidate({
  root,
  domain,
  sources,
  previous,
  previousByIdentity = new Map(),
  doiDelayMs = 2100,
  zenodoToken = process.env.ZENODO_TOKEN,
}) {
  previous ??= await readPreviousCatalog(root);
  const releases = [];
  const sourceRoots = {};
  const sourceCommits = {};
  for (const source of sources) {
    sourceCommits[source.repository] = source.sourceCommit;
    for (const release of source.releases) {
      const tool = release.path.split("/")[1];
      const metadata = source.metadata.get(tool);
      if (!metadata.domains.includes(domain)) continue;
      releases.push({ path: release.path, data: { ...release.data, repository: source.repository } });
      sourceRoots[tool] ??= {};
      for (const name of Object.keys(release.data.apps))
        sourceRoots[tool][name] = { root: source.sourceRoot, recipe: metadata.recipe };
    }
  }
  const base = buildCatalog(releases, previous, previousByIdentity);
  const [dois, licenses] = await Promise.all([
    zenodoToken ? collectDoiMetadata(base, releases, {
      delayMs: doiDelayMs,
      token: zenodoToken,
    }) : Promise.resolve({}),
    collectLicenseMetadata(base, sourceRoots),
  ]);
  const files = await makeFiles(
    applyMetadata(base, dois, licenses),
    sourceCommits,
    releases,
  );
  await mkdir(root, { recursive: true });
  for (const [name, content] of Object.entries(files)) {
    const destination = join(root, name);
    const temporary = `${destination}.tmp`;
    await writeFile(temporary, content);
    await rename(temporary, destination);
  }
  return files;
}
