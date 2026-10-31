#!/usr/bin/env python3
"""Page-by-page check of the home stack's live reads.

Against a running `home/` stack with `DEV_MODE=1` in `home/pager.env`:

1. wipes the store and seeds two families through `/api/dev/db` -- users, devices, locations,
   SMS log, messages, conversations, alerts, allow edges, backends,
   settings -- with the fields `firestore.rules` decides on;
2. for a member, a family admin and a super (sessions from `/api/dev/token`)
   opens `GET /api/live` through nginx and subscribes each page's queries,
   built exactly as `web/lib/live/queries.ts` builds them;
3. compares each snapshot with the documents `firestore.rules` admits, written
   out by hand below (403 where the rules refuse the query outright), and
   checks that one new message reaches the right stream as `added`.

    relay/.venv/bin/python tools/live_check.py [--base https://localhost]

Exits non-zero on any mismatch; prints one line per page and role.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
CA = REPO / "home" / "data" / "certs" / "ca" / "ca.crt"


USERS = {
    "sup": ("super", "famA"),
    "adm": ("admin", "famA"),
    "mem": ("member", "famA"),
    "kid": ("member", "famA"),
    "out": ("member", "famB"),
    "badm": ("admin", "famB"),
}


def seed_docs() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        "families/famA": {"name": "Alpha", "blockedNumbers": [], "createdBy": "system"},
        "families/famB": {"name": "Beta", "blockedNumbers": [], "createdBy": "system"},
        "settings/retention": {"messages": {"n": 4, "unit": "weeks"}},
    }
    for uid, (role, fam) in USERS.items():
        docs[f"users/{uid}"] = {
            "uid": uid,
            "alias": f"lc{uid}",
            "displayName": uid,
            "role": role,
            "familyId": fam,
            "kind": "person",
            "disabled": False,
        }
    for dev, owner, fam, locatable in (
        ("dk", "kid", "famA", ["mem"]),
        ("dm", "mem", "famA", []),
        ("dout", "out", "famB", []),
    ):
        docs[f"devices/{dev}"] = {
            "ownerUid": owner,
            "familyId": fam,
            "locatableBy": locatable,
            "label": dev,
        }
    docs["devices/dk/locations/l1"] = {"createdAt": 1, "lat": 1.0, "lon": 2.0}
    docs["devices/dk/locations/l2"] = {"createdAt": 2, "lat": 1.5, "lon": 2.5}
    docs["devices/dout/locations/lo"] = {"createdAt": 1, "lat": 9.0, "lon": 9.0}
    docs["devices/dk/smsLog/s1"] = {"ts": 1, "body": "hi"}
    for mid, sender, to, key, fams, seq in (
        ("m1", "mem", "kid", "k1", ["famA"], 1),
        ("m2", "kid", "mem", "k1", ["famA"], 2),
        ("m3", "out", "kid", "k2", ["famA", "famB"], 3),
        ("m4", "out", "badm", "k3", ["famB"], 4),
    ):
        docs[f"messages/{mid}"] = {
            "senderUid": sender,
            "recipientUid": to,
            "uids": sorted([sender, to]),
            "convKey": key,
            "familyIds": fams,
            "seq": seq,
            "kind": "text",
            "body": mid,
        }
    for key, uids, fams in (
        ("k1", ["kid", "mem"], ["famA"]),
        ("k2", ["kid", "out"], ["famA", "famB"]),
        ("k3", ["badm", "out"], ["famB"]),
    ):
        docs[f"conversations/{key}"] = {
            "uids": uids,
            "familyIds": fams,
            "kind": "dm",
            "lastSeq": 0,
        }
    docs["families/famA/alerts/a1"] = {"status": "open", "ts": 1, "kind": "sms_unknown"}
    docs["families/famA/alerts/a2"] = {
        "status": "handled",
        "ts": 2,
        "kind": "sms_unknown",
    }
    docs["families/famB/alerts/b1"] = {"status": "open", "ts": 1, "kind": "sms_unknown"}
    for edge, frm, to, fams in (
        ("kid_mem", "kid", "mem", ["famA"]),
        ("out_kid", "out", "kid", ["famA", "famB"]),
        ("badm_out", "badm", "out", ["famB"]),
    ):
        docs[f"allow/{edge}"] = {
            "fromUid": frm,
            "toUid": to,
            "familyIds": fams,
            "message": True,
        }
    docs["users/mem/backends/bk1"] = {"kind": "gchat"}
    docs["users/adm/backends/bk2"] = {"kind": "gchat"}
    return docs


def open_alerts(f: str) -> dict:
    return {"collection": f"families/{f}/alerts", "where": [["status", "==", "open"]]}


def family_members(f: str) -> dict:
    return {"collection": "users", "where": [["familyId", "==", f]]}


def my_conversations(uid: str) -> dict:
    return {"collection": "conversations", "where": [["uids", "array-contains", uid]]}


def devices_locatable_by(f: str, uid: str) -> dict:
    return {
        "collection": "devices",
        "where": [["familyId", "==", f], ["locatableBy", "array-contains", uid]],
    }


def devices_owned_by(f: str, uid: str) -> dict:
    return {
        "collection": "devices",
        "where": [["familyId", "==", f], ["ownerUid", "==", uid]],
    }


def family_devices(f: str) -> dict:
    return {"collection": "devices", "where": [["familyId", "==", f]]}


def thread_messages(key: str, uid: str) -> dict:
    return {
        "collection": "messages",
        "where": [["convKey", "==", key], ["uids", "array-contains", uid]],
        "orderBy": [["seq", "desc"]],
        "limit": 50,
    }


def monitor_messages(key: str, f: str) -> dict:
    return {
        "collection": "messages",
        "where": [["convKey", "==", key], ["familyIds", "array-contains", f]],
        "orderBy": [["seq", "desc"]],
        "limit": 50,
    }


def family_alerts(f: str, handled: bool) -> dict:
    c = f"families/{f}/alerts"
    if handled:
        return {"collection": c, "orderBy": [["ts", "desc"]]}
    return {
        "collection": c,
        "where": [["status", "==", "open"]],
        "orderBy": [["ts", "desc"]],
    }


def locations(d: str) -> dict:
    return {
        "collection": f"devices/{d}/locations",
        "orderBy": [["createdAt", "desc"]],
        "limit": 1500,
    }


def coll(c: str) -> dict:
    return {"collection": c}


def doc(p: str) -> dict:
    return {"doc": p}


DENY = "403"
ALL_USERS = sorted(USERS)

# (page, query builder taking the signed-in uid, expected per role). Expected lists are compared
# in order when the query has `orderBy`, else as a set; derived from relay/firestore.rules.
CHECKS: list[tuple[str, Any, dict[str, Any]]] = [
    (
        "AppShell: open alerts badge",
        lambda u: open_alerts("famA"),
        {"mem": DENY, "adm": ["a1"], "sup": ["a1"]},
    ),
    (
        "family-context: families",
        lambda u: coll("families"),
        {"mem": DENY, "adm": DENY, "sup": ["famA", "famB"]},
    ),
    (
        "directory: family members",
        lambda u: family_members("famA"),
        {r: ["adm", "kid", "mem", "sup"] for r in ("mem", "adm", "sup")},
    ),
    (
        "directory: my conversations",
        my_conversations,
        {"mem": ["k1"], "adm": [], "sup": []},
    ),
    (
        "locatableDevices: granted",
        lambda u: devices_locatable_by("famA", u),
        {"mem": ["dk"], "adm": [], "sup": []},
    ),
    (
        "locatableDevices: own",
        lambda u: devices_owned_by("famA", u),
        {"mem": ["dm"], "adm": [], "sup": []},
    ),
    (
        "locatableDevices: family-wide",
        lambda u: family_devices("famA"),
        {"mem": DENY, "adm": ["dk", "dm"], "sup": ["dk", "dm"]},
    ),
    (
        "NotificationWatcher: conversations",
        my_conversations,
        {"mem": ["k1"], "adm": [], "sup": []},
    ),
    (
        "NotificationWatcher: open alerts",
        lambda u: open_alerts("famA"),
        {"mem": DENY, "adm": ["a1"], "sup": ["a1"]},
    ),
    (
        "settings/backends: own",
        lambda u: coll(f"users/{u}/backends"),
        {"mem": ["bk1"], "adm": ["bk2"], "sup": []},
    ),
    (
        "settings/backends: someone else's",
        lambda u: coll("users/adm/backends" if u != "adm" else "users/mem/backends"),
        {"mem": DENY, "adm": DENY, "sup": ["bk2"]},
    ),
    (
        "chat/view: conversation doc (cross-family DM)",
        lambda u: doc("conversations/k2"),
        {"mem": DENY, "adm": ["k2"], "sup": ["k2"]},
    ),
    (
        "chat/view: monitor messages",
        lambda u: monitor_messages("k2", "famA"),
        {"mem": DENY, "adm": ["m3"], "sup": ["m3"]},
    ),
    (
        "chat/[alias]: thread messages",
        lambda u: thread_messages("k1", u),
        {"mem": ["m2", "m1"], "adm": [], "sup": []},
    ),
    (
        "chat/[alias]: convKey-only thread (rules deny)",
        lambda u: {"collection": "messages", "where": [["convKey", "==", "k1"]]},
        {"mem": DENY, "adm": DENY, "sup": ["m1", "m2"]},
    ),
    (
        "chat/[alias]: one message (getLiveOnce)",
        lambda u: doc("messages/m1"),
        {"mem": ["m1"], "adm": ["m1"], "sup": ["m1"]},
    ),
    (
        "chat/[alias]: other family's message",
        lambda u: doc("messages/m4"),
        {"mem": DENY, "adm": DENY, "sup": ["m4"]},
    ),
    (
        "chat/[alias]: peer device (admin shape)",
        lambda u: devices_owned_by("famA", "kid"),
        {"mem": DENY, "adm": ["dk"], "sup": ["dk"]},
    ),
    (
        "chat list: family conversations",
        lambda u: {
            "collection": "conversations",
            "where": [["familyIds", "array-contains", "famA"]],
        },
        {"mem": DENY, "adm": ["k1", "k2"], "sup": ["k1", "k2"]},
    ),
    (
        "location: kid's fixes",
        lambda u: locations("dk"),
        {"mem": ["l2", "l1"], "adm": ["l2", "l1"], "sup": ["l2", "l1"]},
    ),
    (
        "location: other family's fixes",
        lambda u: locations("dout"),
        {"mem": DENY, "adm": DENY, "sup": ["lo"]},
    ),
    (
        "devices/[id]: smsLog (not a page; rules check)",
        lambda u: coll("devices/dk/smsLog"),
        {"mem": DENY, "adm": ["s1"], "sup": ["s1"]},
    ),
    (
        "family/people: members",
        lambda u: family_members("famA"),
        {r: ["adm", "kid", "mem", "sup"] for r in ("mem", "adm", "sup")},
    ),
    (
        "family/people: devices",
        lambda u: family_devices("famA"),
        {"mem": DENY, "adm": ["dk", "dm"], "sup": ["dk", "dm"]},
    ),
    (
        "family/people: other family's members",
        lambda u: family_members("famB"),
        {"mem": DENY, "adm": DENY, "sup": ["badm", "out"]},
    ),
    (
        "family/alerts: all",
        lambda u: family_alerts("famA", True),
        {"mem": DENY, "adm": ["a2", "a1"], "sup": ["a2", "a1"]},
    ),
    (
        "family/alerts: open",
        lambda u: family_alerts("famA", False),
        {"mem": DENY, "adm": ["a1"], "sup": ["a1"]},
    ),
    (
        "family/contacts: allow edges",
        lambda u: {
            "collection": "allow",
            "where": [["familyIds", "array-contains", "famA"]],
        },
        {"mem": DENY, "adm": ["kid_mem", "out_kid"], "sup": ["kid_mem", "out_kid"]},
    ),
    (
        "family/devices: devices",
        lambda u: family_devices("famA"),
        {"mem": DENY, "adm": ["dk", "dm"], "sup": ["dk", "dm"]},
    ),
    (
        "admin/allowlist + users + families + devices: users",
        lambda u: coll("users"),
        {"mem": DENY, "adm": DENY, "sup": ALL_USERS},
    ),
    (
        "admin/allowlist: allow",
        lambda u: coll("allow"),
        {"mem": DENY, "adm": DENY, "sup": ["badm_out", "kid_mem", "out_kid"]},
    ),
    (
        "admin/settings: retention",
        lambda u: doc("settings/retention"),
        {"mem": ["retention"], "adm": ["retention"], "sup": ["retention"]},
    ),
    (
        "admin/families + devices: devices",
        lambda u: coll("devices"),
        {"mem": DENY, "adm": DENY, "sup": ["dk", "dm", "dout"]},
    ),
    (
        "devices/[id]: kid's device",
        lambda u: doc("devices/dk"),
        {"mem": ["dk"], "adm": ["dk"], "sup": ["dk"]},
    ),
    (
        "devices/[id]: other family's device",
        lambda u: doc("devices/dout"),
        {"mem": DENY, "adm": DENY, "sup": ["dout"]},
    ),
    (
        "NewChatDialog: own user doc",
        lambda u: doc(f"users/{u}"),
        {"mem": ["mem"], "adm": ["adm"], "sup": ["sup"]},
    ),
    (
        "ChatSubscribeDialog: owner's user doc",
        lambda u: doc("users/kid"),
        {"mem": ["kid"], "adm": ["kid"], "sup": ["kid"]},
    ),
    (
        "ChatSubscribeDialog: other family's user doc",
        lambda u: doc("users/out"),
        {"mem": DENY, "adm": DENY, "sup": ["out"]},
    ),
    (
        "BridgePhonesSection: family members",
        lambda u: family_members("famA"),
        {r: ["adm", "kid", "mem", "sup"] for r in ("mem", "adm", "sup")},
    ),
]


class LiveStream:
    def __init__(self, client: httpx.Client, base: str, token: str) -> None:
        self.client = client
        self.base = base
        self.headers = {"Authorization": f"Bearer {token}"}
        self.id = uuid.uuid4().hex
        self.events: queue.Queue[dict] = queue.Queue()
        self.opened = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self.opened.wait(10):
            raise SystemExit("live stream did not open")

    def _run(self) -> None:
        with self.client.stream(
            "GET",
            f"{self.base}/api/live",
            params={"stream": self.id},
            headers=self.headers,
            timeout=httpx.Timeout(10, read=None),
        ) as resp:
            if resp.status_code != 200:
                raise SystemExit(f"GET /api/live -> {resp.status_code}")
            self.opened.set()
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    self.events.put(json.loads(line[6:]))

    def subscribe(self, sub: str, q: dict) -> int:
        r = self.client.post(
            f"{self.base}/api/live/{self.id}/subscribe",
            json={"sub": sub, "query": q},
            headers=self.headers,
        )
        return r.status_code

    def wait(self, sub: str, kind: str, timeout: float = 5.0) -> dict:
        while True:
            ev = self.events.get(timeout=timeout)
            if ev["sub"] == sub and ev["type"] == kind:
                return ev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    args = ap.parse_args()
    client = httpx.Client(verify=str(CA), timeout=15)

    # Wipe first so re-runs see the same seed (DEV_MODE only).
    client.post(f"{args.base}/api/dev/db/wipe").raise_for_status()
    for path, data in seed_docs().items():
        r = client.put(f"{args.base}/api/dev/db/doc", json={"path": path, "data": data})
        r.raise_for_status()

    streams = {}
    for role in ("mem", "adm", "sup"):
        r = client.post(f"{args.base}/api/dev/token", json={"uid": role})
        r.raise_for_status()
        streams[role] = LiveStream(client, args.base, r.json()["token"])

    failures = 0
    for n, (page, build, expected) in enumerate(CHECKS):
        for role, want in expected.items():
            st = streams[role]
            q = build(role)
            sub = f"c{n}"
            status = st.subscribe(sub, q)
            if want == DENY:
                got: Any = DENY if status == 403 else f"HTTP {status}"
            elif status != 200:
                got = f"HTTP {status}"
            else:
                ids = [d["id"] for d in st.wait(sub, "snapshot")["docs"]]
                got = ids if q.get("orderBy") else sorted(ids)
            ok = got == (want if want == DENY or q.get("orderBy") else sorted(want))
            failures += not ok
            print(
                f"{'ok  ' if ok else 'FAIL'} {page:55s} {role:4s} {got}{'' if ok else f' (want {want})'}"
            )

    # One change through the feed: a new message in mem<->kid's thread.
    for role in ("mem", "adm"):
        assert streams[role].subscribe("live", thread_messages("k1", role)) == 200
        streams[role].wait("live", "snapshot")
    new_id = f"m{uuid.uuid4().hex[:8]}"  # unique: a re-run must see `added`
    new = seed_docs()["messages/m2"] | {"seq": 9, "body": "new"}
    client.put(
        f"{args.base}/api/dev/db/doc", json={"path": f"messages/{new_id}", "data": new}
    ).raise_for_status()
    ev = streams["mem"].wait("live", "added")
    ok = ev["id"] == new_id and ev["data"]["body"] == "new"
    failures += not ok
    print(
        f"{'ok  ' if ok else 'FAIL'} {'feed: new message reaches the party':55s} mem  {ev['type']} {ev['id']}"
    )
    try:
        streams["adm"].wait("live", "added", timeout=1.5)
        print(
            f"FAIL {'feed: new message withheld from non-party query':55s} adm  got an event"
        )
        failures += 1
    except queue.Empty:
        print(f"ok   {'feed: new message withheld from non-party query':55s} adm  none")

    print(f"\n{len(CHECKS)} queries x 3 roles + 2 feed checks; failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
