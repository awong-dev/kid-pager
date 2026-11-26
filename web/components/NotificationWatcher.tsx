"use client";

/** Shows a browser notification for new messages (and, for admins, new open alerts) when the
 * tab is hidden or the thread is not open. Renders nothing. */

import { withBase } from "@/lib/basePath";
import { useEffect, useRef } from "react";

import { usePathname } from "@/lib/router";
import { useAuth } from "@/lib/auth-context";
import { useDirectory } from "@/lib/directory";
import { useFamily } from "@/lib/family-context";
import { subscribeLive } from "@/lib/live";
import { myConversationsQuery, openAlertsQuery } from "@/lib/live/queries";
import {
  notificationPermission,
  notificationsSupported,
  registerForPush,
  showForegroundMessageNotification,
} from "@/lib/notifications";
import { peerLabel } from "@/lib/names";
import type { AlertDoc, ConversationDoc } from "@/lib/types";

/** Alerts show the relay-provided `preview` and link to `/alerts`. */
function showForegroundAlertNotification(preview: string): void {
  if (notificationPermission() !== "granted") return;
  const n = new Notification("New family alert", {
    body: preview,
    icon: withBase("/icons/icon-192.png"),
    tag: "pager-alerts",
  });
  n.onclick = () => {
    window.focus();
    window.location.href = withBase("/alerts");
  };
}

function currentThreadAlias(pathname: string | null): string | null {
  if (!pathname) return null;
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length < 2 || parts[0] !== "chat") return null;
  return decodeURIComponent(parts[1]!);
}

/** Like `showForegroundMessageNotification`, but titled and linked for a group. */
function showForegroundGroupNotification(alias: string, name: string, preview: string): void {
  if (notificationPermission() !== "granted") return;
  const n = new Notification(`New message in ${name}`, {
    body: preview,
    icon: withBase("/icons/icon-192.png"),
    tag: `pager-thread-${alias}`,
  });
  n.onclick = () => {
    window.focus();
    window.location.href = withBase(`/chat/${encodeURIComponent(alias)}`);
  };
}

export default function NotificationWatcher() {
  const { me, isFamilyAdmin } = useAuth();
  const { byUid } = useDirectory();
  const { familyId } = useFamily();
  const pathname = usePathname();

  const pathnameRef = useRef(pathname);
  useEffect(() => {
    pathnameRef.current = pathname;
  }, [pathname]);

  const byUidRef = useRef(byUid);
  useEffect(() => {
    byUidRef.current = byUid;
  }, [byUid]);

  // Refresh the push registration once per page load, only when permission is already
  // "granted" so registerForPush()'s requestPermission() cannot show a prompt.
  const pushRefreshed = useRef(false);
  useEffect(() => {
    if (!me || pushRefreshed.current) return;
    if (!notificationsSupported() || notificationPermission() !== "granted") return;
    pushRefreshed.current = true;
    registerForPush().catch((err: unknown) => console.warn("push registration refresh failed", err));
  }, [me]);

  useEffect(() => {
    if (!me) return;
    const previousUnread = new Map<string, number>();
    let initialized = false;

    const unsubscribe = subscribeLive<ConversationDoc>(myConversationsQuery(me.uid), (docs) => {
      docs.forEach((d) => {
        const data = d.data;
        const unread = data.unread?.[me.uid] ?? 0;
        const prevUnread = previousUnread.get(d.id) ?? 0;

        if (initialized && unread > prevUnread) {
          const openAlias = currentThreadAlias(pathnameRef.current);

          if (data.kind === "group") {
            const alias = data.alias;
            if (alias !== undefined) {
              const threadOpen = alias === openAlias;
              if (document.visibilityState !== "visible" || !threadOpen) {
                showForegroundGroupNotification(alias, data.name ?? alias, data.lastPreview);
              }
            }
          } else {
            const peerUid = data.uids.find((u) => u !== me.uid) ?? data.uids[0]!;
            const part = data.participants?.[peerUid];
            const dir = byUidRef.current(peerUid);
            const alias = part?.alias ?? dir?.alias;
            const title = alias
              ? peerLabel({ alias, displayName: part?.displayName || dir?.displayName, kind: part?.kind ?? dir?.kind })
              : undefined;
            const threadOpen = alias !== undefined && alias === openAlias;
            if (alias !== undefined && (document.visibilityState !== "visible" || !threadOpen)) {
              showForegroundMessageNotification(alias, data.lastPreview, title);
            }
          }
        }
        previousUnread.set(d.id, unread);
      });
      initialized = true;
    });

    return unsubscribe;
  }, [me]);

  useEffect(() => {
    if (!isFamilyAdmin || !familyId) return;
    const seen = new Set<string>();
    let initialized = false;

    const unsubscribe = subscribeLive<AlertDoc>(openAlertsQuery(familyId), (docs) => {
      docs.forEach((d) => {
        if (initialized && !seen.has(d.id)) {
          const data = d.data;
          const onAlertsPage = pathnameRef.current?.startsWith("/alerts") ?? false;
          if (document.visibilityState !== "visible" || !onAlertsPage) {
            showForegroundAlertNotification(data.preview);
          }
        }
        seen.add(d.id);
      });
      initialized = true;
    });

    return unsubscribe;
  }, [isFamilyAdmin, familyId]);

  return null;
}
