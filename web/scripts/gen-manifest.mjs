#!/usr/bin/env node
// Writes the generated `public/manifest.webmanifest` from `sw/manifest.template.json`.
// Home production build (`gen-manifest.mjs build`, NEXT_PUBLIC_BACKEND=home): the literal
// placeholder `/__PAGER_BASE_PATH__` stays in every URL; the relay rewrites it at startup to the
// runtime prefix (see lib/buildBase.ts). Any other build, and `next dev`: the placeholder is
// removed, so the manifest is the plain root-served one.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import nextEnv from "@next/env";
const { loadEnvConfig } = nextEnv;

const webDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const isBuild = process.argv[2] === "build";
loadEnvConfig(webDir, !isBuild);

const PLACEHOLDER = "/__PAGER_BASE_PATH__";
const keep = isBuild && process.env.NEXT_PUBLIC_BACKEND === "home";
const text = readFileSync(join(webDir, "sw", "manifest.template.json"), "utf8");
const out = keep ? text : text.split(PLACEHOLDER).join("");
const outPath = join(webDir, "public", "manifest.webmanifest");
writeFileSync(outPath, out);
console.log(`gen-manifest: wrote ${outPath} (${keep ? "placeholder kept" : "no prefix"})`);
