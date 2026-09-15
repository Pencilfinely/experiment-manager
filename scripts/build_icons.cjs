/* Regenerate Windows icons from the approved SVG artwork. Requires sharp. */
'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');

const assets = path.resolve(__dirname, '../assets');
// Caption/tray (16 px) and taskbar (32 px) at Windows' 25% scale steps.
const sizes = [16, 20, 24, 28, 32, 36, 40, 44, 48, 56, 64, 72, 80, 88, 96, 112, 128, 256];

// Runtime sizes use 32-bit DIB for .NET Framework. The Windows shell's
// 256 px frame uses lossless PNG compression.
function dib(rgba, size) {
  const maskStride = Math.ceil(size / 32) * 4;
  const pixelsLength = size * size * 4;
  const result = Buffer.alloc(40 + pixelsLength + maskStride * size);
  result.writeUInt32LE(40, 0);
  result.writeInt32LE(size, 4);
  result.writeInt32LE(size * 2, 8);
  result.writeUInt16LE(1, 12);
  result.writeUInt16LE(32, 14);
  result.writeUInt32LE(pixelsLength + maskStride * size, 20);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const source = (y * size + x) * 4;
      const target = 40 + ((size - 1 - y) * size + x) * 4;
      result[target] = rgba[source + 2];
      result[target + 1] = rgba[source + 1];
      result[target + 2] = rgba[source];
      result[target + 3] = rgba[source + 3];
      if (rgba[source + 3] === 0) {
        result[40 + pixelsLength + (size - 1 - y) * maskStride + (x >> 3)] |= 0x80 >> (x & 7);
      }
    }
  }
  return result;
}

async function build(name) {
  const source = await fs.readFile(path.join(assets, `${name}.svg`));
  const frames = [];
  for (const size of sizes) {
    const png = await sharp(source, { density: 72 * size / 48 })
      .resize(size, size).ensureAlpha().png().toBuffer();
    await fs.writeFile(path.join(assets, 'generated', `${name}-${size}.png`), png);
    const data = size === 256 ? png : dib(await sharp(png).ensureAlpha().raw().toBuffer(), size);
    frames.push({ size, data });
  }
  const directory = Buffer.alloc(6 + 16 * frames.length);
  directory.writeUInt16LE(1, 2);
  directory.writeUInt16LE(frames.length, 4);
  let offset = directory.length;
  frames.forEach(({ size, data }, index) => {
    const entry = 6 + index * 16;
    directory[entry] = directory[entry + 1] = size === 256 ? 0 : size;
    directory.writeUInt16LE(1, entry + 4);
    directory.writeUInt16LE(32, entry + 6);
    directory.writeUInt32LE(data.length, entry + 8);
    directory.writeUInt32LE(offset, entry + 12);
    offset += data.length;
  });
  await fs.writeFile(path.join(assets, `${name}.ico`), Buffer.concat([directory, ...frames.map(frame => frame.data)]));
  console.log(`Built ${name}.ico (${sizes.join(', ')} px)`);
}

async function main() {
  await fs.mkdir(path.join(assets, 'generated'), { recursive: true });
  for (const name of ['center', 'worker']) await build(name);
  await fs.copyFile(path.join(assets, 'center.ico'), path.resolve(__dirname, '../expman/static/favicon.ico'));
  await fs.writeFile(path.join(assets, 'generated', 'renderer.json'), JSON.stringify({
    sharp: sharp.versions.sharp, rsvg: sharp.versions.rsvg, sizes,
  }, null, 2) + '\n');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
