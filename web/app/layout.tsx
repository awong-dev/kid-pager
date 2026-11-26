import type { Metadata, Viewport } from "next";

import Providers from "@/components/Providers";
import { BUILD_BASE } from "@/lib/buildBase";

import "./globals.css";

export const metadata: Metadata = {
  title: "Pager",
  description: "School pickup pager -- web app",
  manifest: `${BUILD_BASE}/manifest.webmanifest`,
  icons: {
    // explicit: declaring `apple` here suppresses the automatic link for app/icon.svg
    icon: [{ url: `${BUILD_BASE}/icon.svg`, type: "image/svg+xml" }],
    apple: `${BUILD_BASE}/icons/apple-touch-icon.png`,
  },
};

export const viewport: Viewport = {
  themeColor: "#0b3c6f",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // `data-pager-base` is how client code learns the runtime prefix (lib/basePath.ts).
    <html lang="en" data-pager-base={BUILD_BASE}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
