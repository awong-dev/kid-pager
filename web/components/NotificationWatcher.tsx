"use client";

/** Shows a browser notification for new messages (and, for admins, new open alerts) when the

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";

function currentThreadAlias(pathname: string | null): string | null {
  if (!pathname) return null;
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length < 2 || parts[0] !== "chat") return null;
  return decodeURIComponent(parts[1]!);
}

export default function NotificationWatcher() {
  const pathname = usePathname();

  const pathnameRef = useRef(pathname);
  useEffect(() => {
    pathnameRef.current = pathname;
  }, [pathname]);

  useEffect(() => {

  useEffect(() => {
    if (!me) return;
    const previousUnread = new Map<string, number>();
    let initialized = false;

        const unread = data.unread?.[me.uid] ?? 0;
        const prevUnread = previousUnread.get(d.id) ?? 0;

        if (initialized && unread > prevUnread) {
          const openAlias = currentThreadAlias(pathnameRef.current);
          }
        }
        previousUnread.set(d.id, unread);
      });
      initialized = true;
    });

    return unsubscribe;
  }, [me]);

  return null;
}
