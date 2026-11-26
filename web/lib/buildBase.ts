/** Build-time URL prefix, for server-rendered files only (`app/layout.tsx`, `app/manifest.ts`,
 * `next.config.ts`). NEVER import this from client code: use `lib/basePath.ts`, which reads the
 * prefix at runtime.
 *
 * The home production build emits this literal placeholder everywhere the prefix belongs; the
 * relay rewrites it in the exported text files at startup, to `/pager` (say) or to `""`. Every
 * other build (Firebase Hosting, `next dev`) has no prefix and no placeholder. */
export const BASE_PLACEHOLDER = "/__PAGER_BASE_PATH__";

export const BUILD_BASE: string =
  process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_BACKEND === "home" ? BASE_PLACEHOLDER : "";
