/** Server shell for `/chat/[alias]`: declares static params; the page is `ThreadPageClient.tsx`. */

import ThreadPageClient from "./ThreadPageClient";

export function generateStaticParams() {
  return [{ alias: "_" }];
}

// Must be a literal `false` under `output: 'export'`; hosting rewrites `/chat/<alias>` to `_`.
export const dynamicParams = false;

export default function ChatThreadPage() {
  return <ThreadPageClient />;
}
