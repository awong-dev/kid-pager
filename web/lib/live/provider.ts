/** The build's live provider: `NEXT_PUBLIC_BACKEND=firebase|home`, default firebase. */

import { firebaseProvider } from "./firebaseProvider";
import { sseProvider } from "./sseProvider";
import type { LiveProvider } from "./types";

export const liveProvider: LiveProvider =
  process.env.NEXT_PUBLIC_BACKEND === "home" ? sseProvider : firebaseProvider;
