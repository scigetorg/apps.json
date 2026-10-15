import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { readFile, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import {
  imageBuild,
  makeFiles,
  readPreviousCatalog,
  readReleases,
  readRetiredImages,
  retiredImagesText,
} from "./catalog.mjs";
import { readDomains } from "./workflow.mjs";

const exec = promisify(execFile);

export async function retireDomain({ root, domain, image, sourceRoot, sourceCommit }) {
  const domainRoot = join(root, domain);
  const [previous, retiredImages, releases, log, manifestText] = await Promise.all([
    readPreviousCatalog(domainRoot),
    readRetiredImages(domainRoot),
    readReleases(sourceRoot),
    readFile(join(domainRoot, "logs.txt"), "utf8"),
    readFile(join(domainRoot, "manifest.json"), "utf8"),
  ]);
  const manifest = JSON.parse(manifestText);
  if (manifest.source_commit !== sourceCommit) {
    throw new Error(`Source checkout does not match ${domain}/manifest.json source_commit`);
  }
  const lines = log ? log.trimEnd().split("\n") : [];
  if (manifest.sha256 !== createHash("sha256").update(log).digest("hex") ||
      manifest.entries !== lines.length) {
    throw new Error(`${domain}/logs.txt does not match its manifest`);
  }

  const affected = [];
  for (const { path, data } of releases) {
    const [, tool, release] = /^releases\/([^/]+)\/([^/]+)\.json$/.exec(path);
    for (const [name, app] of Object.entries(data.apps)) {
      if (imageBuild(tool, release, app) !== image) continue;
      const current = previous.catalog[tool]?.apps?.[name];
      if (!current || current.version !== app.version) {
        throw new Error(`Catalog entry missing or mismatched for ${tool}/${name}`);
      }
      affected.push({ tool, name });
    }
  }
  if (!affected.length) throw new Error(`Image ${image} is absent from release JSON`);
  if (retiredImages.has(image)) return { changed: false, affected };
  if (lines.filter((line) => line.split(" ", 1)[0] === image).length !== 1) {
    throw new Error(`Image ${image} must appear exactly once in ${domain}/logs.txt`);
  }

  const catalog = structuredClone(previous.catalog);
  for (const { tool, name } of affected) {
    catalog[tool].apps[name].show_in_menu = false;
    catalog[tool].apps[name].show_in_applist = false;
  }
  retiredImages.add(image);
  const files = await makeFiles(catalog, sourceCommit, releases, retiredImages);
  files["retired-images.json"] = retiredImagesText(retiredImages);
  for (const [name, content] of Object.entries(files)) {
    const destination = join(domainRoot, name);
    const temporary = `${destination}.tmp`;
    await writeFile(temporary, content);
    await rename(temporary, destination);
  }
  return { changed: true, affected };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [domain, image] = process.argv.slice(2);
  if (!domain || !image || process.argv.length !== 4) {
    throw new Error("Usage: node scripts/retire.mjs <domain> <image_builddate>");
  }
  const root = process.cwd();
  const domains = await readDomains(root);
  if (!Object.hasOwn(domains, domain)) throw new Error(`Unknown domain: ${domain}`);
  const sourceRoot = join(root, ".catalog-sources", domain);
  const sourceCommit = (await exec("git", ["-C", sourceRoot, "rev-parse", "HEAD"])).stdout.trim();
  const result = await retireDomain({ root, domain, image, sourceRoot, sourceCommit });
  console.log(`${result.changed ? "Retired" : "Already retired"} ${image} in ${domain}`);
  for (const { tool, name } of result.affected) console.log(`  ${tool}: ${name}`);
}
