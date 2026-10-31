/** A live-read query for either provider; `doc` and `collection` are slash-joined paths. */

export type WhereOp = "==" | "array-contains";
export type WhereClause = [field: string, op: WhereOp, value: unknown];
export type OrderClause = [field: string, dir: "asc" | "desc"];

export type LiveQuery =
  | { doc: string }
  | {
      collection: string;
      group?: boolean;
      where?: WhereClause[];
      orderBy?: OrderClause[];
      limit?: number;
    };

/** One document as both providers deliver it; `data` keeps Firestore types (`Timestamp`). */
export interface LiveDoc<T> {
  id: string;
  path: string;
  data: T;
}

export type LiveUnsubscribe = () => void;

/** `onNext` gets the full current result after every change, ordered and limited. */
export interface LiveProvider {
  subscribe(
    q: LiveQuery,
    onNext: (docs: LiveDoc<unknown>[]) => void,
    onError: (err: Error) => void
  ): LiveUnsubscribe;
}

export function isDocQuery(q: LiveQuery): q is { doc: string } {
  return "doc" in q;
}
