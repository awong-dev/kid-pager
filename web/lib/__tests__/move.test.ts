import { describe, expect, it } from "vitest";

import {
  type AccountDraft,
  type AccountPerson,
  type BeamInfo,
  type SourceDeviceRow,
  type TargetDeviceRow,
  bannerKind,
  beamActions,
  beamDeviceLine,
  beamSwitchBackBlock,
  beamTryTargets,
  beamWaitingWarning,
  bearerTryResultText,
  waitingFromRefusal,
  type BeamDevice,
  bridgeStillHere,
  bridgeTally,
  caFingerprintOf,
  checkAccounts,
  confirmOf,
  formatDuration,
  groupHex,
  caPemOf,
  changedDetails,
  freezeProblems,
  isTlsFailure,
  reportOfRefusal,
  normalizeAdminMove,
  normalizeTargetUrl,
  parseInviteFragment,
  passphraseProblem,
  passwordChangeProblem,
  peopleTally,
  preflightWarnings,
  remedyFor,
  sortSource,
  sortTarget,
  sourceDetail,
  sourceStage,
  sourceTally,
  targetTally,
  tallyText,
  toMillis,
} from "../move";

const src = (o: Partial<SourceDeviceRow> = {}): SourceDeviceRow => ({ deviceId: "d", label: "Pager", capable: true, ...o });
const tgt = (o: Partial<TargetDeviceRow> = {}): TargetDeviceRow => ({
  deviceId: "d", label: "Pager", state: "waiting", remedy: "none", ...o,
});

describe("sourceStage", () => {
  it("walks from not capable to moved", () => {
    expect(sourceStage(src({ capable: false }))).toBe("incapable");
    expect(sourceStage(src())).toBe("ready");
    expect(sourceStage(src({ sentAt: "2026-01-01T00:00:00Z" }))).toBe("ordered");
    expect(sourceStage(src({ sentAt: "x", ackAt: "y" }))).toBe("acked");
    expect(sourceStage(src({ sentAt: "x", ackAt: "y", last: { st: "pend" } }))).toBe("acked");
    expect(sourceStage(src({ sentAt: "x", ackAt: "y", last: { st: "try", at: 1 } }))).toBe("trying");
    expect(sourceStage(src({ sentAt: "x", ackAt: "y", last: { st: "ok" } }))).toBe("moved");
  });
  it("a failure and a cancel win over an ack", () => {
    expect(sourceStage(src({ ackAt: "y", last: { st: "fail", rsn: "tls" } }))).toBe("failed");
    expect(sourceStage(src({ ackAt: "y", cancelledAt: "z" }))).toBe("cancelled");
    expect(sourceStage(src({ last: { st: "cxl" } }))).toBe("cancelled");
  });
  it("says why a pager failed and how often it tried", () => {
    const d = sourceDetail(src({ ackAt: "y", last: { st: "fail", rsn: "ca", at: 3 } }));
    expect(d).toContain("certificate");
    expect(d).toContain("3 tries");
    expect(sourceDetail(src({ ackAt: "y", last: { st: "fail", rsn: "weird" as never, at: 1 } }))).toContain("weird");
  });
  it("a failed try that will be repeated reads as acked with a reason", () => {
    expect(sourceDetail(src({ ackAt: "y", last: { st: "pend", rsn: "conn" } }))).toContain("will try again");
  });
});

describe("source summary and order", () => {
  const rows = [
    src({ deviceId: "a", label: "A", last: { st: "ok" }, ackAt: "y" }),
    src({ deviceId: "b", label: "B", last: { st: "fail", rsn: "tls" }, ackAt: "y" }),
    src({ deviceId: "c", label: "C", capable: false }),
    src({ deviceId: "d", label: "D", ackAt: "y" }),
  ];
  it("counts by stage and says how many need a person", () => {
    const t = sourceTally(rows);
    expect(t.total).toBe(4);
    expect(t.needsPerson).toBe(2);
    expect(tallyText(t)).toBe("4 pagers: 1 moved, 1 have the order, 1 failed, 1 cannot move by itself");
  });
  it("has words for none and one", () => {
    expect(tallyText(sourceTally([]))).toBe("No pagers.");
    expect(tallyText(sourceTally([src()]))).toBe("1 pager: 1 ready");
  });
  it("lists failures first and moved last", () => {
    expect(sortSource(rows).map((r) => r.label)).toEqual(["B", "C", "D", "A"]);
  });
});

describe("target states and remedies", () => {
  it("only a not-arrived pager with a remedy gets one", () => {
    const ctx = { host: "home.example", sourceUp: true };
    expect(remedyFor(tgt({ remedy: "none" }), ctx)).toBeNull();
    expect(remedyFor(tgt({ state: "moved", remedy: "switch_now_or_setup" }), ctx)).toBeNull();
    expect(remedyFor(tgt({ remedy: "setup_code" }), ctx)).toMatchObject({ setupCode: true });
  });
  it("names both remedies, worded for whether the old server answers", () => {
    const up = remedyFor(tgt({ remedy: "switch_now_or_setup" }), { host: "home.example", sourceUp: true })!;
    expect(up.text).toContain("Check the old server's status page");
    expect(up.text).toContain('"Switch to home.example now"');
    expect(up.text).toContain("setup code");
    const gone = remedyFor(tgt({ remedy: "switch_now_or_setup" }), { host: "home.example", sourceUp: false })!;
    expect(gone.text).not.toContain("status page");
    expect(gone.text).toContain('"Switch to home.example now"');
    expect(gone.setupCode).toBe(true);
  });
  it("counts arrived, set up again, not yet and dark", () => {
    const rows = [
      tgt({ state: "moved" }),
      tgt({ state: "reprovisioned" }),
      tgt({ state: "trying" }),
      tgt({ state: "waiting", darkSince: "2026-01-01T00:00:00Z", remedy: "switch_now_or_setup" }),
      tgt({ state: "waiting" }),
    ];
    const t = targetTally(rows);
    expect(t.arrived).toBe(2);
    expect(t.dark).toBe(1);
    expect(t.needsPerson).toBe(1);
    expect(tallyText(t)).toBe("5 pagers: 1 arrived, 1 set up again, 1 arriving, 2 not yet, 1 dark");
  });
  it("puts dark pagers that need a person first and arrived last", () => {
    const rows = [
      tgt({ label: "arrived", state: "moved" }),
      tgt({ label: "waiting" }),
      tgt({ label: "dark", remedy: "setup_code", darkSince: "2026-01-01T00:00:00Z" }),
      tgt({ label: "remedy", remedy: "setup_code" }),
    ];
    expect(sortTarget(rows).map((r) => r.label)).toEqual(["dark", "remedy", "waiting", "arrived"]);
  });
});

describe("accounts panel", () => {
  const ann: AccountPerson = { uid: "u1", alias: "ann", displayName: "Ann", email: "ann@x.org" };
  const bob: AccountPerson = { uid: "u2", alias: "bob", displayName: "Bob", email: null };
  const draft = (o: Partial<AccountDraft>): AccountDraft => ({ email: "", noSignIn: false, setPassword: false, password: "", ...o });

  it("home needs nothing from anyone", () => {
    const c = checkAccounts("home", [ann, bob], {});
    expect(c.ok).toBe(true);
    expect(c.answers).toEqual([{ uid: "u1" }, { uid: "u2" }]);
  });
  it("home: a password, if set, needs 8 characters", () => {
    const c = checkAccounts("home", [ann], { u1: draft({ setPassword: true, password: "short" }) });
    expect(c.ok).toBe(false);
    expect(c.errors.u1).toContain("8");
    const ok = checkAccounts("home", [ann], { u1: draft({ setPassword: true, password: "longenough" }) });
    expect(ok.answers).toEqual([{ uid: "u1", password: "longenough" }]);
  });
  it("firebase: an email, or No sign-in, for everyone; stored emails are the default", () => {
    const c = checkAccounts("firebase", [ann, bob], {});
    expect(c.ok).toBe(false);
    expect(Object.keys(c.errors)).toEqual(["u2"]);
    const fixed = checkAccounts("firebase", [ann, bob], { u2: draft({ noSignIn: true }) });
    expect(fixed.ok).toBe(true);
    expect(fixed.answers).toEqual([{ uid: "u1", email: "ann@x.org" }, { uid: "u2", noSignIn: true }]);
  });
  it("firebase: rejects a malformed email and one used twice", () => {
    expect(checkAccounts("firebase", [bob], { u2: draft({ email: "nope" }) }).errors.u2).toContain("not an email");
    const dup = checkAccounts("firebase", [ann, bob], { u2: draft({ email: "ANN@x.org" }) });
    expect(dup.errors.u2).toContain("already");
  });
  it("reads what a Freeze 422 names", () => {
    expect(freezeProblems({ detail: "x", missing: ["a", "b"] }).missing).toEqual(["a", "b"]);
    expect(freezeProblems({ detail: "x", invalid: { u1: "bad", u2: 3 } }).invalid).toEqual({ u1: "bad" });
    expect(freezeProblems({ detail: "x", unknown: ["z"] }).unknown).toEqual(["z"]);
    expect(freezeProblems("oops")).toEqual({ missing: [], invalid: {}, unknown: [] });
  });
  it("home: a password is 8 to 128 characters", () => {
    const long = checkAccounts("home", [ann], { u1: draft({ setPassword: true, password: "x".repeat(129) }) });
    expect(long.errors.u1).toContain("128");
    expect(checkAccounts("home", [ann], { u1: draft({ setPassword: true, password: "x".repeat(128) }) }).ok).toBe(true);
  });
  it("reads the changed details of a Freeze 409", () => {
    expect(changedDetails({ detail: "d", broker: { host: "h", port: 8883 }, caSha: "ab" })).toEqual({ host: "h", port: 8883, caSha: "ab" });
    expect(changedDetails({ detail: "d", broker: { host: "h", port: 1 } })?.caSha).toBeNull();
    expect(changedDetails({ detail: "a move is already under way" })).toBeNull();
  });
});

describe("target address and confirmation", () => {
  it("normalises to an https base URL with its path", () => {
    expect(normalizeTargetUrl("home.example/kid-pager/")).toEqual({ url: "https://home.example/kid-pager" });
    expect(normalizeTargetUrl(" https://home.example?x=1#y ")).toEqual({ url: "https://home.example" });
  });
  it("refuses any http address, credentials and junk", () => {
    expect(normalizeTargetUrl("http://home.example")).toHaveProperty("error");
    expect(normalizeTargetUrl("http://localhost:8001")).toHaveProperty("error");
    expect(normalizeTargetUrl("https://u:p@home.example")).toHaveProperty("error");
    expect(normalizeTargetUrl("")).toHaveProperty("error");
    expect(normalizeTargetUrl("https://")).toHaveProperty("error");
  });
  const info = {
    v: 1, authBackend: "home" as const, schemaVersion: 2, relayVersion: "1", baseUrl: "https://h",
    broker: { host: "h", port: 8883 }, ca: { url: "https://h/ca", sha: "ab".repeat(32) },
    beam: null, empty: true, move: null,
  };
  it("confirms exactly the host, port and fingerprint shown", () => {
    expect(confirmOf(info)).toEqual({ host: "h", port: 8883, caSha: "ab".repeat(32) });
    expect(caFingerprintOf({ ...info, caFingerprint: "cd" })).toBe("cd");
    expect(confirmOf({ ...info, ca: null }).caSha).toBeNull();
  });
  it("warns about a target that is not empty or has no CA", () => {
    expect(preflightWarnings(info)).toEqual([]);
    expect(preflightWarnings({ ...info, empty: false, ca: null })).toHaveLength(2);
  });
  it("groups a fingerprint for reading", () => {
    expect(groupHex("0123456789abcdef")).toBe("01234567 89abcdef");
  });
  it("checks the bundle passphrase", () => {
    expect(passphraseProblem("short")).toContain("8");
    expect(passphraseProblem("longenough", "different")).toContain("differ");
    expect(passphraseProblem("päss-wörd-ok")).toContain("ASCII");
    expect(passphraseProblem("longenough", "longenough")).toBeNull();
  });
});

describe("bridges, people and Beam", () => {
  const now = Date.parse("2026-01-01T12:00:00Z");
  it("a bridge is still here only while it keeps arriving", () => {
    expect(bridgeStillHere({ bridgeId: "b", arrived: true, lastSeenAt: "2026-01-01T11:58:00Z" }, now)).toBe(true);
    expect(bridgeStillHere({ bridgeId: "b", arrived: true, lastSeenAt: "2026-01-01T11:30:00Z" }, now)).toBe(false);
    expect(bridgeStillHere({ bridgeId: "b", arrived: false, lastSeenAt: "2026-01-01T11:59:00Z" }, now)).toBe(false);
  });
  it("tallies bridges as here, not arrived, or quiet", () => {
    const t = bridgeTally(
      [
        { bridgeId: "1", arrived: true, lastSeenAt: "2026-01-01T11:59:00Z" },
        { bridgeId: "2", arrived: false },
        { bridgeId: "3", arrived: true, lastSeenAt: "2026-01-01T09:00:00Z" },
      ],
      now,
    );
    expect(t).toEqual({ here: 1, notYet: 1, gone: 1, total: 3 });
  });
  it("tallies people who have and have not signed in", () => {
    expect(
      peopleTally([
        { uid: "1", alias: "a", signedIn: true },
        { uid: "2", alias: "b" },
        { uid: "3", alias: "c", noSignIn: true },
      ]),
    ).toEqual({ signedIn: 1, notYet: 1, noSignIn: 1 });
  });
  const beam = (o: Partial<BeamInfo>): BeamInfo => ({
    count: 2, movedCount: 0, waitingCount: 0, onBeamCount: 0, switched: false, soracom: true, devices: [], ...o,
  });
  it("Switch back is open unless a pager is on Beam here", () => {
    expect(beamActions(beam({}))).toMatchObject({ canSwitch: true, canSwitchBack: false });
    expect(beamActions(beam({ switched: true }))).toMatchObject({ canSwitch: false, canSwitchBack: true });
    expect(beamActions(beam({ switched: true, movedCount: 1 })).canSwitchBack).toBe(true);
    expect(beamActions(beam({ switched: true, onBeamCount: 1 })).canSwitchBack).toBe(false);
    expect(beamSwitchBackBlock(beam({ switched: true, onBeamCount: 1 }))).toContain("on Beam here");
    expect(beamSwitchBackBlock(beam({ switched: true, onBeamCount: 2 }))).toContain("2 pagers are on Beam");
    expect(beamSwitchBackBlock(beam({ switched: true, movedCount: 1 }))).toBeNull();
    expect(beamSwitchBackBlock(beam({ switched: true }))).toBeNull();
    expect(beamActions(beam({ switched: true, byHand: true }))).toMatchObject({ canSwitchBack: false, switchBackByHand: true });
  });
  it("waiting pagers make the switch need a confirmation", () => {
    expect(beamActions(beam({ waitingCount: 0 })).switchNeedsForce).toBe(false);
    expect(beamActions(beam({ waitingCount: 2 })).switchNeedsForce).toBe(true);
    expect(beamWaitingWarning(beam({ waitingCount: 1 }))).toContain("1 Beam pager has not arrived");
    expect(beamWaitingWarning(beam({ waitingCount: 3 }))).toContain("3 Beam pagers have not arrived");
    expect(beamWaitingWarning(beam({}))).toBeNull();
    expect(waitingFromRefusal({ detail: "x", waiting: ["a", 2, "b"] })).toEqual(["a", "b"]);
    expect(waitingFromRefusal({ detail: "x" })).toEqual([]);
    expect(waitingFromRefusal(null)).toEqual([]);
  });
  const dev = (o: Partial<BeamDevice>): BeamDevice => ({ deviceId: "d1", label: "Kit", bearer: "direct", canTry: true, try: null, ...o });
  it("step 2 asks the pagers that are not on Beam and can try", () => {
    const list = [
      dev({ deviceId: "a" }),
      dev({ deviceId: "b", bearer: "beam" }),
      dev({ deviceId: "c", canTry: false }),
      dev({ deviceId: "d", bearer: null }),
    ];
    expect(beamTryTargets(list)).toEqual(["a", "d"]);
    expect(beamActions(beam({ switched: true, devices: list })).canTryBeam).toBe(true);
    expect(beamActions(beam({ switched: false, devices: list })).canTryBeam).toBe(false);
    expect(beamActions(beam({ switched: true, devices: [dev({ bearer: "beam" })] })).canTryBeam).toBe(false);
  });
  it("words each pager's bearer and try outcome", () => {
    expect(beamDeviceLine(dev({ label: null }))).toMatchObject({ name: "d1", bearer: "Direct", outcome: null });
    expect(beamDeviceLine(dev({ bearer: null })).bearer).toBe("Unknown");
    expect(beamDeviceLine(dev({ try: { st: "sent", rsn: null } })).outcome).toBe("Asked, waiting for the pager");
    expect(beamDeviceLine(dev({ try: { st: "try", rsn: null } })).outcome).toBe("Trying Beam");
    expect(beamDeviceLine(dev({ bearer: "beam", try: { st: "ok", rsn: null } }))).toMatchObject({ bearer: "Beam", outcome: "On Beam" });
    expect(beamDeviceLine(dev({ try: { st: "fail", rsn: "conn" } })).outcome).toBe("Went back to direct: could not connect through Beam");
    expect(beamDeviceLine(dev({ try: { st: "fail", rsn: "auth" } })).outcome).toContain("another server");
    expect(beamDeviceLine(dev({ try: { st: "fail", rsn: "proof" } })).outcome).toContain("did not answer");
    expect(beamDeviceLine(dev({ try: { st: "fail", rsn: null } })).outcome).toBe("Went back to direct");
    expect(beamDeviceLine(dev({ try: { st: "na", rsn: null } })).outcome).toBe("Not a Beam SIM");
    expect(beamDeviceLine(dev({ canTry: false })).outcome).toBe("Firmware too old to switch");
    expect(beamDeviceLine(dev({ canTry: false, bearer: "beam" })).outcome).toBeNull();
  });
  it("sums up the answer of the ask", () => {
    expect(bearerTryResultText({ sent: ["a", "b"], skipped: [] })).toBe("2 pagers asked to try Beam.");
    expect(bearerTryResultText({ sent: [], skipped: [] })).toBe("No pager was asked.");
    expect(bearerTryResultText({ sent: ["a"], skipped: [{ id: "c", reason: "firmware" }] }, () => "Kit")).toBe(
      "1 pager asked to try Beam. Skipped: Kit (firmware too old).",
    );
  });
  it("there is nothing to switch with no Beam pagers", () => {
    expect(beamActions(beam({ count: 0 })).canSwitch).toBe(false);
  });
});

describe("answers and helpers", () => {
  it("tells idle, source and target apart", () => {
    expect(normalizeAdminMove(null).role).toBe("idle");
    expect(normalizeAdminMove({ id: null, state: "none" }).role).toBe("idle");
    const s = normalizeAdminMove({ id: "mv_1", state: "frozen", target: { baseUrl: "https://t" }, devices: [{}] });
    expect(s).toMatchObject({ role: "source", bridges: [], people: [] });
    const t = normalizeAdminMove({ id: "mv_1", state: "imported", sourceBaseUrl: "https://s" });
    expect(t).toMatchObject({ role: "target", fedLinks: [], beam: null });
  });
  it("reads times as ISO text, seconds or milliseconds", () => {
    expect(toMillis("2026-01-01T00:00:00Z")).toBe(Date.parse("2026-01-01T00:00:00Z"));
    expect(toMillis(1_767_225_600)).toBe(1_767_225_600_000);
    expect(toMillis(1_767_225_600_000)).toBe(1_767_225_600_000);
    expect(toMillis(null)).toBeNull();
    expect(toMillis("junk")).toBeNull();
  });
  it("formats how long a pager has been dark", () => {
    expect(formatDuration(20_000)).toBe("under 1m");
    expect(formatDuration(35 * 60_000)).toBe("35m");
    expect(formatDuration(130 * 60_000)).toBe("2h 10m");
    expect(formatDuration(27 * 3_600_000)).toBe("1d 3h");
  });
  it("picks the banner: claim link, email, or plain", () => {
    expect(bannerKind({ targetBaseUrl: "https://t", claim: { url: "https://t/login#invite=x" } })).toBe("claim");
    expect(bannerKind({ targetBaseUrl: "https://t", email: "a@b.c" })).toBe("email");
    expect(bannerKind({ targetBaseUrl: "https://t" })).toBe("plain");
  });
  it("reads the claim code from the login fragment", () => {
    expect(parseInviteFragment("#invite=ABCD-1234")).toBe("ABCD-1234");
    expect(parseInviteFragment("#x=1&invite=a%20b")).toBeNull();
    expect(parseInviteFragment("#invite=")).toBeNull();
    expect(parseInviteFragment("")).toBeNull();
    expect(parseInviteFragment("#other=1")).toBeNull();
  });
  it("checks the first-sign-in password change", () => {
    expect(passwordChangeProblem("", "newpassword", "newpassword")).toContain("gave you");
    expect(passwordChangeProblem("old", "short", "short")).toContain("8");
    expect(passwordChangeProblem("oldpassword", "oldpassword", "oldpassword")).toContain("different");
    expect(passwordChangeProblem("oldpassword", "newpassword", "other")).toContain("differ");
    expect(passwordChangeProblem("oldpassword", "newpassword", "newpassword")).toBeNull();
  });
});

describe("private CA and refusals", () => {
  it("accepts only PEM certificate text", () => {
    const pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----";
    expect(caPemOf(`\n${pem}\n`)).toBe(pem);
    expect(caPemOf("not a cert")).toBeNull();
    expect(caPemOf("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")).toBeNull();
  });
  it("recognises a TLS failure of the check", () => {
    expect(isTlsFailure(502, "the new server did not answer over TLS: its web certificate is not from a public CA: give its CA certificate (caPem)")).toBe(true);
    expect(isTlsFailure(502, "the new server did not answer: unreachable")).toBe(false);
    expect(isTlsFailure(502, "the new server did not answer over TLS: the CA certificate (caPem) does not match the new server's web certificate")).toBe(true);
    expect(isTlsFailure(422, "the new server did not answer over TLS: x")).toBe(false);
  });
  it("finds the report inside a refused import", () => {
    expect(reportOfRefusal({ detail: "refused", ok: false, errors: ["e"], collisions: [{ kind: "alias", id: "a" }] })?.errors).toEqual(["e"]);
    expect(reportOfRefusal({ detail: "this server already has a move on record" })).toBeNull();
  });
  it("cancelled and cannot-move rows say why they need a setup code", () => {
    expect(sourceDetail(src({ cancelledAt: "z" }))).toContain("setup code");
    expect(sourceDetail(src({ capable: false }))).not.toContain("firmware");
  });
});
