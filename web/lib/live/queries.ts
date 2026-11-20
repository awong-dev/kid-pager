/** Every live read the web app makes, as a `LiveQuery` builder. */

import type { LiveQuery } from "./types";

/** Open alerts of one family. */
export function openAlertsQuery(familyId: string): LiveQuery {
  return { collection: `families/${familyId}/alerts`, where: [["status", "==", "open"]] };
}

/** Every family. */
export function allFamiliesQuery(): LiveQuery {
  return { collection: "families" };
}

/** Users of one family. */
export function familyMembersQuery(familyId: string): LiveQuery {
  return { collection: "users", where: [["familyId", "==", familyId]] };
}

/** Conversations this account is a party to. */
export function myConversationsQuery(uid: string): LiveQuery {
  return { collection: "conversations", where: [["uids", "array-contains", uid]] };
}

/** Family devices this account holds a `locate` grant on. */
export function familyDevicesLocatableByQuery(familyId: string, uid: string): LiveQuery {
  return {
    collection: "devices",
    where: [
      ["familyId", "==", familyId],
      ["locatableBy", "array-contains", uid],
    ],
  };
}

/** Family devices owned by `ownerUid`. */
export function familyDevicesOwnedByQuery(familyId: string, ownerUid: string): LiveQuery {
  return {
    collection: "devices",
    where: [
      ["familyId", "==", familyId],
      ["ownerUid", "==", ownerUid],
    ],
  };
}

/** Every device of one family. */
export function familyDevicesQuery(familyId: string): LiveQuery {
  return { collection: "devices", where: [["familyId", "==", familyId]] };
}

/** This account's delivery backends. */
export function userBackendsQuery(uid: string): LiveQuery {
  return { collection: `users/${uid}/backends` };
}

/** One conversation doc. */
export function conversationQuery(convKey: string): LiveQuery {
  return { doc: `conversations/${convKey}` };
}

/** Newest `pageSize` messages for a family admin who is not a party; `firestore.rules`
 * authorises the list by the `familyIds` clause. */
export function monitorMessagesQuery(convKey: string, familyId: string, pageSize: number): LiveQuery {
  return {
    collection: "messages",
    where: [
      ["convKey", "==", convKey],
      ["familyIds", "array-contains", familyId],
    ],
    orderBy: [["seq", "desc"]],
    limit: pageSize,
  };
}

/** Newest `pageSize` messages of a conversation this account is a party to. The `uids` clause
 * is required by `firestore.rules`, which authorises a list from the declared filters alone. */
export function threadMessagesQuery(convKey: string, uid: string, pageSize: number): LiveQuery {
  return {
    collection: "messages",
    where: [
      ["convKey", "==", convKey],
      ["uids", "array-contains", uid],
    ],
    orderBy: [["seq", "desc"]],
    limit: pageSize,
  };
}

/** One message doc. */
export function messageQuery(id: string): LiveQuery {
  return { doc: `messages/${id}` };
}

/** Every conversation with a member of one family. */
export function familyConversationsQuery(familyId: string): LiveQuery {
  return { collection: "conversations", where: [["familyIds", "array-contains", familyId]] };
}

/** The newest `n` location fixes of one device. */
export function deviceLocationsQuery(deviceId: string, n: number): LiveQuery {
  return { collection: `devices/${deviceId}/locations`, orderBy: [["createdAt", "desc"]], limit: n };
}

/** The family's alerts, newest first; open ones only unless `showHandled`. */
export function familyAlertsQuery(familyId: string, showHandled: boolean): LiveQuery {
  const collection = `families/${familyId}/alerts`;
  return showHandled
    ? { collection, orderBy: [["ts", "desc"]] }
    : { collection, where: [["status", "==", "open"]], orderBy: [["ts", "desc"]] };
}

/** `allow` edges that touch one family. */
export function allowEdgesOfFamilyQuery(familyId: string): LiveQuery {
  return { collection: "allow", where: [["familyIds", "array-contains", familyId]] };
}

/** Every user. */
export function allUsersQuery(): LiveQuery {
  return { collection: "users" };
}

/** Every `allow` edge. */
export function allAllowQuery(): LiveQuery {
  return { collection: "allow" };
}

/** The retention settings doc. */
export function retentionSettingsQuery(): LiveQuery {
  return { doc: "settings/retention" };
}

/** The sweep's proof-of-run doc; readable by any registered user. */
export function metaSettingsQuery(): LiveQuery {
  return { doc: "settings/meta" };
}

/** Every device. */
export function allDevicesQuery(): LiveQuery {
  return { collection: "devices" };
}

/** One device doc. */
export function deviceQuery(deviceId: string): LiveQuery {
  return { doc: `devices/${deviceId}` };
}

/** One user doc. */
export function userQuery(uid: string): LiveQuery {
  return { doc: `users/${uid}` };
}
