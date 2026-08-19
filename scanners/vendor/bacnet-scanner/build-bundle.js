'use strict';
/*
 * build-bundle.js — produces dist/bundle.js: a single self-contained entry file
 * for Node's Single Executable Application (SEA) build. No external bundler is
 * used (this machine blocks spawned build exes), so we do a tiny hand-rolled
 * CommonJS bundle: each local module is wrapped in a function registry, and all
 * web assets (public/*, template/*) are embedded as base64.
 */
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const DIST = path.join(ROOT, 'dist');
fs.mkdirSync(DIST, { recursive: true });

// Local modules to inline. Order doesn't matter (registry is lazy).
const MODULES = ['./vendors.js', './archive.js', './bacnet.js', './server.js'];

// Assets to embed, keyed by forward-slash relative path.
function walk(dir, base, out) {
  for (const name of fs.readdirSync(dir)) {
    const abs = path.join(dir, name);
    const rel = (base ? base + '/' : '') + name;
    if (fs.statSync(abs).isDirectory()) walk(abs, rel, out);
    else out[rel] = fs.readFileSync(abs).toString('base64');
  }
}
const assets = {};
walk(path.join(ROOT, 'public'), 'public', assets);
walk(path.join(ROOT, 'template'), 'template', assets);

let out = '';
out += "'use strict';\n";
out += '// AUTO-GENERATED single-file bundle. Do not edit by hand.\n';

// Embedded assets
out += 'global.__ASSETS__ = (function(){ var A = {};\n';
for (const [k, b64] of Object.entries(assets)) {
  out += `A[${JSON.stringify(k)}] = { enc: 'base64', data: ${JSON.stringify(b64)} };\n`;
}
out += 'return A; })();\n';

// Module registry + custom require (falls back to real require for built-ins)
out += `
var __registry = {};
var __cache = {};
var __realRequire = require;
function __req(id) {
  var key = id;
  if (__registry[key]) {
    if (__cache[key]) return __cache[key].exports;
    var m = { exports: {} };
    __cache[key] = m;
    __registry[key](m, m.exports, __req, __dirname, __filename);
    return m.exports;
  }
  return __realRequire(id);
}
`;

for (const rel of MODULES) {
  const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  // Register under both './name' and './name.js' so either require form works.
  const noExt = rel.replace(/\.js$/, '');
  out += `__registry[${JSON.stringify(noExt)}] = __registry[${JSON.stringify(rel)}] = function(module, exports, require, __dirname, __filename) {\n`;
  out += src + '\n};\n';
}

// Boot the server.
out += "__req('./server');\n";

const outPath = path.join(DIST, 'bundle.js');
fs.writeFileSync(outPath, out);
console.log('Wrote', outPath, '(' + Math.round(out.length / 1024) + ' KB, ' + Object.keys(assets).length + ' assets)');
