"use client";

/** `/`: client-side redirect to `/chat` (a static export has no server redirect). */

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/chat");
  }, [router]);
  return null;
}
