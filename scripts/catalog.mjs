import YAML from "yaml";
import { execFile, spawn } from "node:child_process";
import { access, mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const IMAGE = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;
const TOOL_TEXT_FIELDS = ["citation", "description"];
const TOOL_TEXT_GENERATOR = join("tools", "generate_apps_json.py");

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

// A recipe readme opens with a separator and a "## name/version ##" title, then
// the description, then usage sections. Anything matching SECTION_END ends it.
const SECTION_END =
  /^(##\s|```|Examples?:?|---+$|More documentation|To setup|Licensing|Usage|Citation)/i;
const TITLE = /^(##\s.*|[\w.+-]+\/(\{\{[^}]*\}\}|\S)+)$/;
const PLACEHOLDER = /\{\{\s*(?:context\.)?(\w+)\s*\}\}/g;

// Variables may reference each other, so render until nothing changes. Unknown
// keys stay as written.
function renderPlaceholders(text, context) {
  for (let pass = 0; pass < 5; pass++) {
    const next = text.replace(PLACEHOLDER, (match, key) =>
      typeof context[key] === "string" || typeof context[key] === "number" ? String(context[key]) : match);
    if (next === text) break;
    text = next;
  }
  return text;
}

export function describeReadme(readme, context = {}) {
  if (typeof readme !== "string") return undefined;
  const lines = readme.split("\n").map((line) => line.trim());
  let start = 0;
  while (start < lines.length && (!lines[start] || /^-{3,}$/.test(lines[start]))) start++;
  if (TITLE.test(lines[start] ?? "")) start++;
  const body = [];
  for (const line of lines.slice(start)) {
    if (SECTION_END.test(line)) break;
    body.push(line);
  }
  const text = renderPlaceholders(body.join(" "), context).replace(/\s+/g, " ").trim();
  return text || undefined;
}

async function readOptional(path) {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
}

// Tool-level fields for the catalog: the recipe's description and whether it
// ships an OpenRecon scanner label (OpenReconLabel.json, OpenReconLabel.gpu.json, ...).
async function recipeDetails(directory, document) {
  const variables = Object.fromEntries(
    Object.entries(document.variables ?? {}).filter(([, value]) => typeof value !== "object"),
  );
  const context = { ...variables, name: document.name, version: document.version };
  const description = describeReadme(document.readme, context) ??
    describeReadme(await readOptional(join(directory, "README.md")), context);
  const openrecon = (await readdir(directory)).some((name) => /^OpenReconLabel.*\.json$/.test(name));
  return { ...(description && { description }), ...(openrecon && { openrecon }) };
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
    const details = document ? await recipeDetails(join(sourceRoot, "recipes", recipe), document) : {};
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
    result.set(tool, { domains: labels, recipe, details });
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

// Cross-repository ownership of apps and images is enforced once, across all
// sources, by assertUniqueSources in workflow.mjs before any domain is built.
export function buildCatalog(releases, previous, previousByIdentity = new Map()) {
  const catalog = {};
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
      if (combined.doi === undefined && prior.image === combined.image &&
          prior.version === combined.version && prior.doi !== undefined)
        combined.doi = prior.doi;
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
  const images = new Set();
  for (const { path, data } of releases) {
    const [, tool, release] = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    for (const app of Object.values(data.apps)) images.add(imageBuild(tool, release, app));
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
      entries: images.size,
      source_commits: sourceCommits,
      app_sources: appSources,
    }),
  };
}

export async function collectDoiMetadata(
  catalog,
  releases,
  publishedDois = {},
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
    const doi = publishedDois[title];
    if (doi) {
      for (const [name, app] of related) {
        const resolved = `https://doi.org/${doi}`;
        if (app.doi !== resolved) (found[tool] ??= {})[name] = resolved;
      }
      continue;
    }
    const existing = related.find(([, app]) => app.doi)?.[1].doi;
    if (existing) {
      for (const [name, app] of related)
        if (!app.doi) (found[tool] ??= {})[name] = existing;
    }
  }
  return found;
}

function toolTextFields(entry) {
  const fields = {};
  for (const field of TOOL_TEXT_FIELDS) {
    const value = entry?.[field];
    if (typeof value === "string" && value.trim()) fields[field] = value;
  }
  return fields;
}

// A source repository that ships tools/generate_apps_json.py owns its tools'
// citation and description: its generator renders each recipe for the
// release's variant, so its output is taken rather than re-parsed here.
// Returns {} when the source has no generator; throws when the generator fails.
export async function readToolText(sourceRoot) {
  const generator = join(sourceRoot, TOOL_TEXT_GENERATOR);
  try {
    await access(generator);
  } catch (error) {
    if (error.code === "ENOENT") return {};
    throw error;
  }
  const directory = await mkdtemp(join(tmpdir(), "tool-text-"));
  try {
    const output = join(directory, "apps.json");
    const { ZENODO_TOKEN, ...env } = process.env;
    try {
      await promisify(execFile)("python3",
        [generator, "--releases-dir", join(sourceRoot, "releases"), "--output", output],
        { cwd: sourceRoot, env, maxBuffer: 64 * 1024 * 1024 });
    } catch (error) {
      const detail = error.stderr?.trim().split("\n").at(-1) || error.message;
      throw new Error(`${TOOL_TEXT_GENERATOR} failed: ${detail}`);
    }
    const generated = JSON.parse(await readFile(output, "utf8"));
    if (!generated || typeof generated !== "object" || Array.isArray(generated))
      throw new Error(`${TOOL_TEXT_GENERATOR} did not write an object`);
    const text = {};
    for (const [tool, entry] of Object.entries(generated)) {
      const fields = toolTextFields(entry);
      if (Object.keys(fields).length) text[tool] = fields;
    }
    return text;
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

// Reads every title's license from its recipe history (scripts/recipe_history.py).
// `titles` maps image title -> { repository, root } of its source clone.
export async function resolveRecipeLicenses(titles) {
  if (!Object.keys(titles).length) return { resolved: {}, errors: {} };
  return new Promise((resolve, reject) => {
    const child = spawn("python3", ["-m", "scripts.catalog_licenses"],
      { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) reject(new Error(`License resolver failed: ${stderr.trim()}`));
      else {
        try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
      }
    });
    child.stdin.end(JSON.stringify({ titles }));
  });
}

// Every image's license is read from its recipe history on every run: the
// newest commit declaring its version (local git, no network). The license
// already in apps.json is kept only when that history has none for the version
// (mostly build.sh-era images); otherwise the image has no license.
export async function collectLicenseMetadata(catalog, sourceRoots, {
  previousByImage = new Map(), resolve = resolveRecipeLicenses,
} = {}) {
  const artifacts = new Map();
  for (const [tool, entry] of Object.entries(catalog)) {
    for (const [name, app] of Object.entries(entry.apps)) {
      const source = sourceRoots[tool]?.[name];
      if (!source) continue;
      const title = `${app.image}_${app.version}`;
      const group = artifacts.get(title) ?? {
        recipe: source.recipe, repository: source.repository, root: source.root, apps: [],
      };
      if (group.recipe !== source.recipe || (group.repository && source.repository &&
          group.repository !== source.repository))
        throw new Error(`Conflicting source recipes for ${title}`);
      group.apps.push({ tool, name });
      artifacts.set(title, group);
    }
  }
  const titles = Object.fromEntries([...artifacts].map(([title, group]) =>
    [title, { repository: group.repository ?? "", root: group.root ?? "" }]));
  const { resolved = {}, errors = {} } = await resolve(titles);
  const apps = {};
  let changed = 0;
  let kept = 0;
  const missing = [];
  for (const [title, group] of artifacts) {
    const stored = previousByImage.get(title);
    const fresh = resolved[title];
    let metadata;
    if (fresh?.license.length) {
      metadata = fresh;
      if (stored && JSON.stringify(stored) !== JSON.stringify(fresh)) {
        changed += 1;
        console.log(`License for ${title} from recipe history: ${JSON.stringify(stored.license)} -> ` +
          JSON.stringify(fresh.license));
      }
    } else if (stored) {
      metadata = stored;
      kept += 1;
    } else {
      missing.push(`${title}: ${errors[title] ?? "its recipe declares no license for this version"}`);
      continue;
    }
    for (const { tool, name } of group.apps) (apps[tool] ??= {})[name] = metadata;
  }
  console.log(`Licenses: ${Object.keys(resolved).length} image(s) read from recipe history, ` +
    `${changed} changed, ${kept} kept from apps.json (no recipe license for their version), ` +
    `${missing.length} without a license`);
  for (const line of missing) console.log(`  no license: ${line}`);
  return apps;
}

export function applyMetadata(catalog, dois, licenses, details = {}) {
  const updated = structuredClone(catalog);
  for (const [tool, entry] of Object.entries(updated)) {
    Object.assign(entry, details[tool]);
    for (const [name, app] of Object.entries(entry.apps)) {
      if (dois[tool]?.[name]) app.doi = dois[tool][name];
      if (licenses[tool]?.[name]) {
        const metadata = licenses[tool][name];
        if (Array.isArray(metadata)) app.license = metadata;
        else {
          app.license = metadata.license;
          if (metadata.license_details.length) app.license_details = metadata.license_details;
          else delete app.license_details;
        }
      }
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
  previousByImage = new Map(),
  resolveLicenses = resolveRecipeLicenses,
  publishedDois = {},
}) {
  previous ??= await readPreviousCatalog(root);
  const releases = [];
  const sourceRoots = {};
  const sourceCommits = {};
  const details = {};
  for (const source of sources) {
    sourceCommits[source.repository] = source.sourceCommit;
    for (const release of source.releases) {
      const tool = release.path.split("/")[1];
      const metadata = source.metadata.get(tool);
      if (!metadata.domains.includes(domain)) continue;
      releases.push({ path: release.path, data: { ...release.data, repository: source.repository } });
      // The source generator's text wins over the description parsed here.
      // toolText null: the generator failed, so keep the published text.
      details[tool] = { ...metadata.details, ...toolTextFields(source.toolText === null
        ? previous.catalog[tool] : source.toolText?.[tool]) };
      sourceRoots[tool] ??= {};
      for (const name of Object.keys(release.data.apps))
        sourceRoots[tool][name] = {
          root: source.sourceRoot, recipe: metadata.recipe, repository: source.repository,
        };
    }
  }
  const base = buildCatalog(releases, previous, previousByIdentity);
  const [dois, licenses] = await Promise.all([
    collectDoiMetadata(base, releases, publishedDois),
    collectLicenseMetadata(base, sourceRoots, {
      previousByImage, resolve: resolveLicenses,
    }),
  ]);
  const files = await makeFiles(
    applyMetadata(base, dois, licenses, details),
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
