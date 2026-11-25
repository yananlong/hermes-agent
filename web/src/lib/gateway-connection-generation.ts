/**
 * How many times this page has opened a socket to the dashboard it is served from.
 *
 * A cached `-32601` from `session.foreign.status` is a statement about ONE gateway build. The
 * dashboard is served by the same process as the gateway, so a server restart — the deploy, the
 * upgrade, the profile switch — drops the page's long-lived `/api/events` socket and the client
 * dials again. That dial is the moment the page learns "a different build may be answering now",
 * and it is what `lib/foreign-capability` keys its negative cache on: a verdict from generation N
 * is never consulted in generation N+1.
 *
 * A plain module-level counter with subscribers (rather than a store library) keeps the dependency
 * direction one way: the events feed bumps it, the linked surfaces read it via
 * `useSyncExternalStore`. Nothing here imports anything.
 */

type Listener = () => void

let generation = 0
const listeners = new Set<Listener>()

/** The generation a capability verdict belongs to. */
export function dashboardConnectionGeneration(): number {
  return generation
}

/** A dashboard socket opened (first dial or a reconnect after a drop). */
export function noteDashboardConnectionOpened(): void {
  generation += 1
  for (const listener of listeners) {
    listener()
  }
}

/** Subscribe for `useSyncExternalStore`; returns the unsubscribe. */
export function subscribeDashboardConnection(listener: Listener): () => void {
  listeners.add(listener)

  return () => {
    listeners.delete(listener)
  }
}
