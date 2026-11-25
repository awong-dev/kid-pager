import type { NextConfig } from "next";

import { API_PATH_PREFIXES } from "./lib/apiPaths";

// Dev only: `rewrites()` is ignored by the export build; production gets the same two rules from
// `firebase.json` (API prefixes to the relay, everything else to `/index.html`).
const RELAY = "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "export",

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
