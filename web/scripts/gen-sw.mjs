#!/usr/bin/env node
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
// `@next/env` is published as CommonJS; Node's ESM loader only gives us the
// default export, not the named ones `next` itself gets via `require()`.
import nextEnv from "@next/env";
const { loadEnvConfig } = nextEnv;

const webDir = join(dirname(fileURLToPath(import.meta.url)), "..");

// Same env loader as `next build`, so file precedence matches the app.
loadEnvConfig(webDir, process.env.NODE_ENV !== "production");

const REQUIRED_VARS = [
  "NEXT_PUBLIC_FIREBASE_API_KEY",
  "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
  "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
  "NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET",
  "NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID",
  "NEXT_PUBLIC_FIREBASE_APP_ID",
];

  process.exit(1);
}


    process.exit(1);
  }
}

