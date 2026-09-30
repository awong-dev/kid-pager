/** Server shell for `/chat/view/[key]`: static params; the page is `MonitorThreadClient.tsx`. */

import MonitorThreadClient from "./MonitorThreadClient";

export function generateStaticParams() {
  return [{ key: "_" }];
}

// Must be a literal `false` under `output: 'export'`.
export const dynamicParams = false;

export default function ChatViewPage() {
  return <MonitorThreadClient />;
}
