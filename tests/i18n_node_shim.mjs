// Issue #1341: loads the real frontend/intl-messageformat.iife.js + frontend/i18n.js into a minimal browser-like
// sandbox (no DOM library), so Node tests exercise the same files the dashboard ships.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const FRONTEND = path.join(HERE, '..', 'custom_components', 'climate_advisor', 'frontend');

// opts: {search, navigatorLanguage, hassLanguage, hass (object|null), files ({url: object} overrides), failFetch}
export function loadRuntime(opts = {}) {
  const attrs = {};
  const warns = [];
  const hass = opts.hass !== undefined ? opts.hass : (opts.hassLanguage ? { language: opts.hassLanguage } : null);
  const fakeParent = { document: { querySelector: (sel) => (sel === 'home-assistant' && hass ? { hass } : null) } };
  const sandbox = {
    console: { warn: (m) => warns.push(String(m)), log() {}, error() {} },
    Intl, URLSearchParams, Promise, setTimeout, clearTimeout, AbortController, Object, Array, String, Number, Math,
    Date, JSON, Error,
    location: { search: opts.search || '' },
    navigator: { language: opts.navigatorLanguage },
    document: { documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } },
    parent: fakeParent, top: fakeParent,
    fetch: async (url) => {
      if (opts.failFetch) throw new Error('offline');
      if (opts.files && url in opts.files) return { ok: true, status: 200, json: async () => opts.files[url] };
      const file = path.join(FRONTEND, url);
      if (!fs.existsSync(file)) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => JSON.parse(fs.readFileSync(file, 'utf8')) };
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const f of ['intl-messageformat.iife.js', 'i18n.js']) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, f), 'utf8'), sandbox, { filename: f });
  }
  return { CAI18N: sandbox.CAI18N, attrs, warns, sandbox };
}
