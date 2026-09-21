/** Server shell for `/devices/[id]`: declares static params; the page is `DevicePageClient.tsx`. */

import DevicePageClient from "./DevicePageClient";

export function generateStaticParams() {
  return [{ id: "_" }];
}

// Must be a literal `false` under `output: 'export'`.
export const dynamicParams = false;

export default function DeviceDetailPage() {
  return <DevicePageClient />;
}
