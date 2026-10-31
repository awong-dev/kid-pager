/** Each `LiveQuery` builder against the Firestore query the firebase provider must produce. */

import { initializeApp } from "firebase/app";
import {
  type DocumentReference,
  type Query,
  collection,
  doc,
  getFirestore,
  limit,
  orderBy,
  query,
  queryEqual,
  refEqual,
  where,
} from "firebase/firestore";
import { describe, expect, it } from "vitest";

import { buildFirestoreQuery } from "../firebaseProvider";
import * as Q from "../queries";
import type { LiveQuery } from "../types";

// Never connects: building and comparing queries is local.
const db = getFirestore(initializeApp({ projectId: "demo-live-test", apiKey: "x", appId: "x" }, "live-test"));

function asQuery(q: LiveQuery): Query {
  return buildFirestoreQuery(db, q) as Query;
}

function asRef(q: LiveQuery): DocumentReference {
  return buildFirestoreQuery(db, q) as DocumentReference;
}

describe("harness", () => {
  it("queryEqual discriminates", () => {
    expect(
      queryEqual(
        asQuery(Q.openAlertsQuery("fam1")),
        query(collection(db, "families", "fam2", "alerts"), where("status", "==", "open"))
      )
    ).toBe(false);
  });
});

describe("AppShell", () => {
  it("open alert count", () => {
    const familyId = "fam1";
    const handWritten = query(
      collection(db, "families", familyId, "alerts"),
      where("status", "==", "open")
    );
    expect(queryEqual(asQuery(Q.openAlertsQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("family-context", () => {
  it("all families (super)", () => {
    const handWritten = collection(db, "families");
    expect(queryEqual(asQuery(Q.allFamiliesQuery()), handWritten)).toBe(true);
  });
});

describe("directory", () => {
  it("family members", () => {
    const familyId = "fam1";
    const handWritten = query(collection(db, "users"), where("familyId", "==", familyId));
    expect(queryEqual(asQuery(Q.familyMembersQuery(familyId)), handWritten)).toBe(true);
  });
  it("my group conversations", () => {
    const me = { uid: "u1" };
    const handWritten = query(collection(db, "conversations"), where("uids", "array-contains", me.uid));
    expect(queryEqual(asQuery(Q.myConversationsQuery(me.uid)), handWritten)).toBe(true);
  });
});

describe("locatableDevices", () => {
  const familyId = "fam1";
  const me = { uid: "u1" };
  const devicesRef = collection(db, "devices");
  it("granted", () => {
    const handWritten = query(
      devicesRef,
      where("familyId", "==", familyId),
      where("locatableBy", "array-contains", me.uid)
    );
    expect(queryEqual(asQuery(Q.familyDevicesLocatableByQuery(familyId, me.uid)), handWritten)).toBe(true);
  });
  it("own", () => {
    const handWritten = query(devicesRef, where("familyId", "==", familyId), where("ownerUid", "==", me.uid));
    expect(queryEqual(asQuery(Q.familyDevicesOwnedByQuery(familyId, me.uid)), handWritten)).toBe(true);
  });
  it("family-wide (admin)", () => {
    const handWritten = query(devicesRef, where("familyId", "==", familyId));
    expect(queryEqual(asQuery(Q.familyDevicesQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("NotificationWatcher", () => {
  it("my conversations (unread watcher)", () => {
    const me = { uid: "u1" };
    const handWritten = query(collection(db, "conversations"), where("uids", "array-contains", me.uid));
    expect(queryEqual(asQuery(Q.myConversationsQuery(me.uid)), handWritten)).toBe(true);
  });
  it("open alerts (alert watcher)", () => {
    const familyId = "fam1";
    const handWritten = query(
      collection(db, "families", familyId, "alerts"),
      where("status", "==", "open")
    );
    expect(queryEqual(asQuery(Q.openAlertsQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("settings/backends", () => {
  it("my backends", () => {
    const me = { uid: "u1" };
    const handWritten = collection(db, "users", me.uid, "backends");
    expect(queryEqual(asQuery(Q.userBackendsQuery(me.uid)), handWritten)).toBe(true);
  });
});

describe("chat/view thread", () => {
  const convKey = "a_b";
  it("conversation doc", () => {
    const handWritten = doc(db, "conversations", convKey);
    expect(refEqual(asRef(Q.conversationQuery(convKey)), handWritten)).toBe(true);
  });
  it("messages (family admin)", () => {
    const familyId = "fam1";
    const pageSize = 50;
    const handWritten = query(
      collection(db, "messages"),
      where("convKey", "==", convKey),
      where("familyIds", "array-contains", familyId),
      orderBy("seq", "desc"),
      limit(pageSize)
    );
    expect(queryEqual(asQuery(Q.monitorMessagesQuery(convKey, familyId, pageSize)), handWritten)).toBe(true);
  });
});

describe("chat thread", () => {
  const convKey = "a_b";
  const me = { uid: "u1" };
  it("messages", () => {
    const pageSize = 50;
    const handWritten = query(
      collection(db, "messages"),
      where("convKey", "==", convKey),
      where("uids", "array-contains", me.uid),
      orderBy("seq", "desc"),
      limit(pageSize)
    );
    expect(queryEqual(asQuery(Q.threadMessagesQuery(convKey, me.uid, pageSize)), handWritten)).toBe(true);
  });
  it("peer device (admin)", () => {
    const familyId = "fam1";
    const peerUid = "u2";
    const handWritten = query(
      collection(db, "devices"),
      where("familyId", "==", familyId),
      where("ownerUid", "==", peerUid)
    );
    expect(queryEqual(asQuery(Q.familyDevicesOwnedByQuery(familyId, peerUid)), handWritten)).toBe(true);
  });
  it("peer device (member)", () => {
    const familyId = "fam1";
    const handWritten = query(
      collection(db, "devices"),
      where("familyId", "==", familyId),
      where("locatableBy", "array-contains", me.uid)
    );
    expect(queryEqual(asQuery(Q.familyDevicesLocatableByQuery(familyId, me.uid)), handWritten)).toBe(true);
  });
  it("one-shot message doc", () => {
    const resp = { id: "m1" };
    const handWritten = doc(db, "messages", resp.id);
    expect(refEqual(asRef(Q.messageQuery(resp.id)), handWritten)).toBe(true);
  });
});

describe("chat list", () => {
  const me = { uid: "u1" };
  const familyId = "fam1";
  it("my conversations", () => {
    const handWritten = query(collection(db, "conversations"), where("uids", "array-contains", me.uid));
    expect(queryEqual(asQuery(Q.myConversationsQuery(me.uid)), handWritten)).toBe(true);
  });
  it("my own device", () => {
    const handWritten = query(
      collection(db, "devices"),
      where("familyId", "==", familyId),
      where("ownerUid", "==", me.uid)
    );
    expect(queryEqual(asQuery(Q.familyDevicesOwnedByQuery(familyId, me.uid)), handWritten)).toBe(true);
  });
  it("family conversations (admin)", () => {
    const handWritten = query(collection(db, "conversations"), where("familyIds", "array-contains", familyId));
    expect(queryEqual(asQuery(Q.familyConversationsQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("location", () => {
  it("device location fixes", () => {
    const device = { id: "pgr-1" };
    const LOCATIONS_QUERY_LIMIT = 50;
    const handWritten = query(
      collection(db, "devices", device.id, "locations"),
      orderBy("createdAt", "desc"),
      limit(LOCATIONS_QUERY_LIMIT)
    );
    expect(queryEqual(asQuery(Q.deviceLocationsQuery(device.id, LOCATIONS_QUERY_LIMIT)), handWritten)).toBe(true);
  });
});

describe("family/people", () => {
  const familyId = "fam1";
  it("members", () => {
    const handWritten = query(collection(db, "users"), where("familyId", "==", familyId));
    expect(queryEqual(asQuery(Q.familyMembersQuery(familyId)), handWritten)).toBe(true);
  });
  it("devices", () => {
    const handWritten = query(collection(db, "devices"), where("familyId", "==", familyId));
    expect(queryEqual(asQuery(Q.familyDevicesQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("family/alerts", () => {
  const familyId = "fam1";
  const col = collection(db, "families", familyId, "alerts");
  it("show handled", () => {
    const handWritten = query(col, orderBy("ts", "desc"));
    expect(queryEqual(asQuery(Q.familyAlertsQuery(familyId, true)), handWritten)).toBe(true);
  });
  it("open only", () => {
    const handWritten = query(col, where("status", "==", "open"), orderBy("ts", "desc"));
    expect(queryEqual(asQuery(Q.familyAlertsQuery(familyId, false)), handWritten)).toBe(true);
  });
});

describe("family/contacts", () => {
  it("linked-family allow edges", () => {
    const familyId = "fam1";
    const handWritten = query(collection(db, "allow"), where("familyIds", "array-contains", familyId));
    expect(queryEqual(asQuery(Q.allowEdgesOfFamilyQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("family/devices", () => {
  it("family devices", () => {
    const familyId = "fam1";
    const handWritten = query(collection(db, "devices"), where("familyId", "==", familyId));
    expect(queryEqual(asQuery(Q.familyDevicesQuery(familyId)), handWritten)).toBe(true);
  });
});

describe("admin/allowlist", () => {
  it("users", () => {
    const handWritten = collection(db, "users");
    expect(queryEqual(asQuery(Q.allUsersQuery()), handWritten)).toBe(true);
  });
  it("allow edges", () => {
    const handWritten = collection(db, "allow");
    expect(queryEqual(asQuery(Q.allAllowQuery()), handWritten)).toBe(true);
  });
});

describe("admin/users", () => {
  it("users", () => {
    const handWritten = collection(db, "users");
    expect(queryEqual(asQuery(Q.allUsersQuery()), handWritten)).toBe(true);
  });
});

describe("admin/settings", () => {
  it("retention doc", () => {
    const handWritten = doc(db, "settings", "retention");
    expect(refEqual(asRef(Q.retentionSettingsQuery()), handWritten)).toBe(true);
  });
});

describe("admin/families", () => {
  it("families", () => {
    const handWritten = collection(db, "families");
    expect(queryEqual(asQuery(Q.allFamiliesQuery()), handWritten)).toBe(true);
  });
  it("users", () => {
    const handWritten = collection(db, "users");
    expect(queryEqual(asQuery(Q.allUsersQuery()), handWritten)).toBe(true);
  });
  it("devices", () => {
    const handWritten = collection(db, "devices");
    expect(queryEqual(asQuery(Q.allDevicesQuery()), handWritten)).toBe(true);
  });
});

describe("admin/devices", () => {
  it("devices", () => {
    const handWritten = collection(db, "devices");
    expect(queryEqual(asQuery(Q.allDevicesQuery()), handWritten)).toBe(true);
  });
  it("users", () => {
    const handWritten = collection(db, "users");
    expect(queryEqual(asQuery(Q.allUsersQuery()), handWritten)).toBe(true);
  });
});

describe("devices/[id]", () => {
  it("device doc", () => {
    const id = "pgr-1";
    const handWritten = doc(db, "devices", id);
    expect(refEqual(asRef(Q.deviceQuery(id)), handWritten)).toBe(true);
  });
});

describe("NewChatDialog", () => {
  it("my user doc", () => {
    const meUid = "u1";
    const handWritten = doc(db, "users", meUid);
    expect(refEqual(asRef(Q.userQuery(meUid)), handWritten)).toBe(true);
  });
});

describe("ChatSubscribeDialog", () => {
  it("owner user doc", () => {
    const ownerUid = "u1";
    const handWritten = doc(db, "users", ownerUid);
    expect(refEqual(asRef(Q.userQuery(ownerUid)), handWritten)).toBe(true);
  });
});

describe("BridgePhonesSection", () => {
  it("family members' SMS numbers", () => {
    const familyId = "fam1";
    const handWritten = query(collection(db, "users"), where("familyId", "==", familyId));
    expect(queryEqual(asQuery(Q.familyMembersQuery(familyId)), handWritten)).toBe(true);
  });
});
