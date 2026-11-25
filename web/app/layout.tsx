import type { Metadata, Viewport } from "next";

import Providers from "@/components/Providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Pager",
  description: "School pickup pager -- web app",
  manifest: "/manifest.webmanifest",
  icons: {
    // explicit: declaring `apple` here suppresses the automatic link for app/icon.svg
    icon: [{ url: "/icon.svg", type: "image/svg+xml" }],
    apple: "/icons/apple-touch-icon.png",
  },
};

export const viewport: Viewport = {
  themeColor: "#0b3c6f",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
