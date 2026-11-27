import { dropSessionState } from '@/store/session-states'

import type { ClientSessionState } from '../../../types'

/**
 * A warm cache entry is only trustworthy when it still BELONGS to the session
 * being resumed.
 *
 * A pooled profile backend that gets idle-reaped and respawned
 * (pruneSecondaryGateways) re-mints runtime ids, so a recycled id can resolve to
 * a live-but-DIFFERENT session's cache entry. The `session.activate` 404 guard
 * only catches a fully-DEAD id — a recycled-live id 200s, so an unchecked hit
 * paints the wrong transcript under the current route (the "open chat A, chat B
 * loads" bug). On a mismatch the mapping is cross-wired: purge both sides and
 * report a miss, so the caller falls through to a full resume that rebinds a
 * correct id.
 */
export interface WarmCacheHit {
  runtimeId: string
  state: ClientSessionState
}

export function readWarmCache(
  storedSessionId: string,
  refs: {
    runtimeIdByStoredSessionIdRef: { current: Map<string, string> }
    sessionStateByRuntimeIdRef: { current: Map<string, ClientSessionState> }
  }
): null | WarmCacheHit {
  const runtimeId = refs.runtimeIdByStoredSessionIdRef.current.get(storedSessionId)
  const state = runtimeId ? refs.sessionStateByRuntimeIdRef.current.get(runtimeId) : undefined

  if (!runtimeId || !state) {
    return null
  }

  if (state.storedSessionId !== storedSessionId) {
    refs.runtimeIdByStoredSessionIdRef.current.delete(storedSessionId)
    refs.sessionStateByRuntimeIdRef.current.delete(runtimeId)
    dropSessionState(runtimeId)

    return null
  }

  return { runtimeId, state }
}
