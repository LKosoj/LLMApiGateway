import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

const EXPECTED_MODULES = [
  'core.mjs',
  'fallback.mjs',
  'fusion.mjs',
  'index.mjs',
  'operations.mjs',
  'providers.mjs',
  'router.mjs',
  'state.mjs',
];

test('package pins the deterministic one-file esbuild contract', async () => {
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url)));

  assert.equal(manifest.devDependencies.esbuild, '0.28.1');
  assert.match(manifest.scripts.build, /--format=iife/);
  assert.match(manifest.scripts.build, /--outfile=\.\.\/\.\.\/static\/editor\.js/);
  assert.doesNotMatch(manifest.scripts.build, /--splitting|--minify/);
});

test('source package has one entry and the approved module boundaries', async () => {
  const sourceDirectory = new URL('../src/', import.meta.url);
  const modules = (await readdir(sourceDirectory)).filter(name => name.endsWith('.mjs')).sort();
  const entry = await readFile(new URL('index.mjs', sourceDirectory), 'utf8');

  assert.deepEqual(modules, EXPECTED_MODULES);
  assert.equal((entry.match(/DOMContentLoaded/g) || []).length, 1);
  assert.equal((entry.match(/const ctx =/g) || []).length, 1);
  for (const moduleName of EXPECTED_MODULES.filter(name => !['index.mjs', 'state.mjs'].includes(name))) {
    assert.match(entry, new RegExp(`from './${moduleName}'`));
  }
});

test('state module is the single owner of shared mutable editor state', async () => {
  const sourceDirectory = new URL('../src/', import.meta.url);
  const state = await readFile(new URL('state.mjs', sourceDirectory), 'utf8');

  assert.match(state, /providerModelsCache: new Map\(\)/);
  assert.match(state, /documentBases,/);
  assert.match(state, /saveInFlight: false/);

  for (const moduleName of EXPECTED_MODULES.filter(name => name !== 'state.mjs')) {
    const content = await readFile(new URL(moduleName, sourceDirectory), 'utf8');
    assert.doesNotMatch(content, /\b(?:let|var)\s+(?:activeEditor|saveInFlight|providerModelsCacheEpoch)\b/);
  }
});
