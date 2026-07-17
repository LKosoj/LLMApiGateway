import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { analyzeCatalog, validateRegistry } from "./src/contracts.mjs";

const root = path.resolve(process.argv[2] ?? path.join(import.meta.dirname, "../.."));
const localeRoot = path.join(root, "static", "locales");

async function readJson(file) {
  return JSON.parse(await fs.readFile(file, "utf8"));
}

async function main() {
  const registry = validateRegistry(await readJson(path.join(localeRoot, "registry.json")));
  const localeCodes = new Set(registry.locales.map((locale) => locale.code));
  if (!localeCodes.has("en") || !localeCodes.has("ru")) {
    throw new Error("registry must contain both en and ru locales");
  }
  for (const namespace of registry.namespaces) {
    let reference = null;
    for (const locale of registry.locales) {
      const catalog = await readJson(path.join(localeRoot, locale.code, `${namespace}.json`));
      const analysis = analyzeCatalog(catalog, locale.code, namespace);
      const current = {
        arguments: Object.fromEntries([...analysis.argumentsByKey].sort(([left], [right]) => (
          left.localeCompare(right)
        ))),
        keys: analysis.keys,
      };
      if (reference === null) {
        reference = current;
      } else if (JSON.stringify(current) !== JSON.stringify(reference)) {
        throw new Error(`catalog key or ICU-argument drift: ${locale.code}/${namespace}`);
      }
    }
  }
  console.log("i18n catalogs have valid ICU and matching keys/arguments");
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
