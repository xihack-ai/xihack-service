// Uses the optional sharp package to rasterize the versioned SVG sources.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const packageRoot = process.env.SUBMISSION_NODE_MODULES;
const load = packageRoot ? createRequire(path.join(packageRoot, '..', 'package.json')) : require;
const sharp = load('sharp');
async function main() {
  for (const name of ['system-architecture', 'agent-workflow']) {
    await sharp(fs.readFileSync(path.join(__dirname, name + '.svg'))).png().toFile(path.join(__dirname, name + '.png'));
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
