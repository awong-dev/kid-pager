import type { NextConfig } from "next";

import { API_PATH_PREFIXES } from "./lib/apiPaths";
import { BUILD_BASE } from "./lib/buildBase";

// Dev only: `rewrites()` is ignored by the export build; production gets the same two rules from
// `firebase.json` (API prefixes to the relay, everything else to `/index.html`).
const RELAY = "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "export",

  // Home build: asset URLs carry the placeholder the relay rewrites at startup (lib/buildBase.ts).
  // `assetPrefix` only; the app routes itself (lib/router.tsx) under the runtime prefix.
  ...(BUILD_BASE ? { assetPrefix: BUILD_BASE } : {}),

  async rewrites() {
    return {
      // API paths belong to the relay (the list is lib/apiPaths.ts).
      beforeFiles: API_PATH_PREFIXES.map((p) => ({ source: `${p}/:path*`, destination: `${RELAY}${p}/:path*` })),
      // Everything that is not a file or the page itself is a client route: serve the one page.
      fallback: [{ source: "/:path*", destination: "/" }],
    };
  },
};

export default nextConfig;
