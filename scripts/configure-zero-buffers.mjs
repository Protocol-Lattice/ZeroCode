import { readFileSync, writeFileSync } from 'node:fs';

const path = process.argv[2];
const original = '#define Z_DIRECT_FRAME_LOCAL_LIMIT_BYTES 131072u';
const configured = '#define Z_DIRECT_FRAME_LOCAL_LIMIT_BYTES 16777216u';
const source = readFileSync(path, 'utf8');
if (source.includes(configured)) process.exit(0);
if (source.split(original).length !== 2) {
  throw new Error('Pinned Zero frame-limit definition changed; review the compiler buffer patch.');
}
writeFileSync(path, source.replace(original, configured));
