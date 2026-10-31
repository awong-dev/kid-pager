/** Cloud provider: turns a `LiveQuery` into a Firestore query and listens with `onSnapshot`. */

import {
  type DocumentReference,
  type Firestore,
  type Query,
  type QueryConstraint,
  collection,
  collectionGroup,
  doc,
  limit,
  onSnapshot,
  orderBy,
  query,
  where,
} from "firebase/firestore";

import { getFirestoreDb } from "../firebase";
import { type LiveDoc, type LiveProvider, type LiveQuery, isDocQuery } from "./types";

export function buildFirestoreQuery(db: Firestore, q: LiveQuery): Query | DocumentReference {
  if (isDocQuery(q)) {
    return doc(db, q.doc);
  }
  const constraints: QueryConstraint[] = [];
  for (const [field, op, value] of q.where ?? []) {
    constraints.push(where(field, op, value));
  }
  for (const [field, dir] of q.orderBy ?? []) {
    constraints.push(orderBy(field, dir));
  }
  if (q.limit !== undefined) {
    constraints.push(limit(q.limit));
  }
  const base = q.group ? collectionGroup(db, q.collection) : collection(db, q.collection);
  return constraints.length ? query(base, ...constraints) : base;
}

export const firebaseProvider: LiveProvider = {
  subscribe(q, onNext, onError) {
    const target = buildFirestoreQuery(getFirestoreDb(), q);
    if (isDocQuery(q)) {
      return onSnapshot(
        target as DocumentReference,
        (snap) => onNext(snap.exists() ? [{ id: snap.id, path: snap.ref.path, data: snap.data() }] : []),
        onError
      );
    }
    return onSnapshot(
      target as Query,
      (snap) => {
        const docs: LiveDoc<unknown>[] = snap.docs.map((d) => ({ id: d.id, path: d.ref.path, data: d.data() }));
        onNext(docs);
      },
      onError
    );
  },
};
