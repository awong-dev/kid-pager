import type { NextConfig } from "next";

// `output: 'export'` applies only to a real build: under `next dev`, export's rule that every
// dynamic param be pre-declared rejects `/chat/<alias>` (production rewrites it to one shell).
// `rewrites()` is ignored by the export build; Hosting supplies them in production.
const isDev = process.env.NODE_ENV === "development";

const nextConfig: NextConfig = {
  ...(isDev ? {} : { output: "export" as const }),

  async rewrites() {
    return {
      // Dev stand-in for `firebase.json`'s Hosting rewrites. `beforeFiles` is required because
      // `/chat/[alias]` is a real filesystem route.
      beforeFiles: [
        { source: "/chat/:alias", destination: "/chat/_" },
        { source: "/devices/:id", destination: "/devices/_" },
      ],
      afterFiles: [
        { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
        { source: "/webhooks/:path*", destination: "http://localhost:8000/webhooks/:path*" },
      ],
    };
  },
};

export default nextConfig;
