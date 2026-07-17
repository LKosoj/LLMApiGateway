import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const CHECKER = path.resolve(import.meta.dirname, "../check-catalogs.mjs");

async function runChecker(englishMessage, russianMessage) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "i18n-catalog-checker-"));
  try {
    const localeRoot = path.join(root, "static", "locales");
    await Promise.all([
      fs.mkdir(path.join(localeRoot, "en"), { recursive: true }),
      fs.mkdir(path.join(localeRoot, "ru"), { recursive: true }),
    ]);
    await Promise.all([
      fs.writeFile(path.join(localeRoot, "registry.json"), JSON.stringify({
        schemaVersion: 1,
        defaultLocale: "en",
        locales: [
          { code: "en", nativeLabel: "English", dir: "ltr" },
          { code: "ru", nativeLabel: "Русский", dir: "ltr" },
        ],
        namespaces: ["common"],
        pageNamespaces: { runtime: ["common"] },
      })),
      fs.writeFile(
        path.join(localeRoot, "en", "common.json"),
        JSON.stringify({ items: englishMessage }),
      ),
      fs.writeFile(
        path.join(localeRoot, "ru", "common.json"),
        JSON.stringify({ items: russianMessage }),
      ),
    ]);
    return spawnSync(process.execPath, [CHECKER, root], {
      encoding: "utf8",
    });
  } finally {
    await fs.rm(root, { force: true, recursive: true });
  }
}

test("catalog checker permits locale-specific plural categories", async () => {
  const result = await runChecker(
    "{count, plural, one {# item} other {# items}}",
    "{count, plural, one {# элемент} few {# элемента} many {# элементов} other {# элемента}}",
  );

  assert.equal(result.status, 0, result.stderr);
});

test("catalog checker rejects an ICU argument type mismatch", async () => {
  const result = await runChecker(
    "{count, number}",
    "{count, select, one {Один} other {Другой}}",
  );

  assert.equal(result.status, 1);
  assert.match(result.stderr, /catalog key or ICU-argument drift: ru\/common/);
});
