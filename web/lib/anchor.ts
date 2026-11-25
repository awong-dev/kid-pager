/** `#fragment` anchors: which element id a URL fragment names. Fragments here may carry extra
 * `&key=value` parts (`#federation&link=<id>`, `#fedInvite=<invite>`); only a bare leading word is
 * an anchor. Pure, so it is tested. */

/** The element id a fragment points at (`#contacts` -> "contacts", `#federation&link=x` ->
 * "federation"), or null when the fragment is empty or starts with a `key=value` part. */
export function anchorOf(hash: string): string | null {
  const h = hash.startsWith("#") ? hash.slice(1) : hash;
  const first = h.split("&")[0] ?? "";
  if (!first || first.includes("=")) return null;
  try {
    return decodeURIComponent(first) || null;
  } catch {
    return first;
  }
}
