#!/usr/bin/env node
// Prebuild step: writes the generated `public/sw.js` from `sw/sw.template.js`.
// Home build: drops the FIREBASE-BEGIN/END block. Firebase build: substitutes the six
// `NEXT_PUBLIC_FIREBASE_*` values (and fails if any is missing) and also writes
// `public/firebase-messaging-sw.js`.
import { readFileSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
// `@next/env` is published as CommonJS; Node's ESM loader only gives us the
// default export, not the named ones `next` itself gets via `require()`.
import nextEnv from "@next/env";
const { loadEnvConfig } = nextEnv;

const webDir = join(dirname(fileURLToPath(import.meta.url)), "..");

// Same env loader as `next build`, so file precedence matches the app.
loadEnvConfig(webDir, process.env.NODE_ENV !== "production");

const home = process.env.NEXT_PUBLIC_BACKEND === "home";

// The values the compat SDK's `firebase.initializeApp()` needs.
const REQUIRED_VARS = [
  "NEXT_PUBLIC_FIREBASE_API_KEY",
  "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
  "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
  "NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET",
  "NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID",
  "NEXT_PUBLIC_FIREBASE_APP_ID",
];

const BEGIN = "// FIREBASE-BEGIN\n";
const END = "// FIREBASE-END\n";

const templatePath = join(webDir, "sw", "sw.template.js");
let template = readFileSync(templatePath, "utf8");
const begin = template.indexOf(BEGIN);
const end = template.indexOf(END);
if (begin < 0 || end < begin) {
  console.error("gen-sw: template is missing the FIREBASE-BEGIN/FIREBASE-END block");
  process.exit(1);
}

const outDir = join(webDir, "public");
mkdirSync(outDir, { recursive: true });
const legacyPath = join(outDir, "firebase-messaging-sw.js");
let outputs;

if (home) {
  template = template.slice(0, begin) + template.slice(end + END.length);
  // A firebase build's leftover must not ship in the home export.
  rmSync(legacyPath, { force: true });
  outputs = [join(outDir, "sw.js")];
} else {
  const missing = REQUIRED_VARS.filter((name) => !process.env[name]);
  if (missing.length > 0) {
    console.error(
      `gen-sw: missing required env var(s) for public/sw.js: ${missing.join(
        ", "
      )}. Copy web/.env.local.example to web/.env.local (or set these in the ` +
        "build environment) -- no demo/placeholder config is ever baked into the " +
        "generated service worker."
    );
    process.exit(1);
  }
  for (const name of REQUIRED_VARS) {
    const placeholder = `__${name}__`;
    if (!template.includes(placeholder)) {
      console.error(`gen-sw: template is missing placeholder ${placeholder}`);
      process.exit(1);
    }
    template = template.split(placeholder).join(process.env[name]);
  }
  template = template.replace(BEGIN, "").replace(END, "");
  outputs = [join(outDir, "sw.js"), legacyPath];
}

for (const outPath of outputs) {
  writeFileSync(outPath, template);
  console.log(`gen-sw: wrote ${outPath}`);
}
