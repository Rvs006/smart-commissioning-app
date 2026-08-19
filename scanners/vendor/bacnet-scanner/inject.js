'use strict';
// Inject the SEA blob into the copied node.exe via postject's programmatic API
// (the CLI would spawn a blocked child exe on this machine, so we call the lib).
const fs = require('fs');
const { inject } = require('postject');

const exePath = 'C:/bacnetscanner/dist/BacnetScanner.exe';
const blob = fs.readFileSync('C:/bacnetscanner/dist/sea-prep.blob');

(async () => {
  await inject(exePath, 'NODE_SEA_BLOB', blob, {
    sentinelFuse: 'NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2',
  });
  console.log('Injected', blob.length, 'bytes into', exePath);
})().catch((e) => { console.error(e); process.exit(1); });
