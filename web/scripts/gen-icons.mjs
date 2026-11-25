// One-off: rasterise public/logo.svg into the PNG icons. Run: node scripts/gen-icons.mjs
import { readFileSync } from "node:fs";
import sharp from "sharp";

const svg = readFileSync(new URL("../public/logo.svg", import.meta.url));
const out = (p) => new URL(`../public/icons/${p}`, import.meta.url).pathname;
const BG = "#fff4d6";

async function plain(size, file) {
  await sharp(svg, { density: (72 * size) / 128 * 2 }).resize(size, size).png().toFile(out(file));
}
await plain(192, "icon-192.png");
await plain(512, "icon-512.png");
// apple-touch-icon: iOS does not round-mask transparency well, so use a solid background
async function solid(size, inner, file) {
  const mark = await sharp(svg, { density: 576 }).resize(inner, inner).png().toBuffer();
  await sharp({ create: { width: size, height: size, channels: 4, background: BG } })
    .composite([{ input: mark, gravity: "center" }])
    .png()
    .toFile(out(file));
}
await solid(180, 150, "apple-touch-icon.png");
// maskable: mark inside the central 80% safe zone
await solid(512, 360, "icon-maskable-512.png");
