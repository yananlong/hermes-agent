import type { ClientSessionState } from '../../../types'

/**
 * What a resume entry decides about the session it is about to open.
 *
 * A linked pointer is a LOCATOR: Hermes holds no transcript for it, so anything
 * already on screen belongs to a different conversation and must not stand in
 * while the native window loads. Two independent reasons to treat the entry as
 * linked-ish: the listed row says so, or the warm cache already holds `codex:`
 * rows. An UNLISTED id also counts, because an absent row cannot prove the
 * session is ordinary.
 *
 * `detachPrevious` releases the previous session's runtime binding and its
 * leftover turn lock. Leaving another chat's runtime bound is #89696: the
 * primary view reads the active runtime's slice and the view sync admits only
 * the active runtime, so a turn still streaming there keeps painting under this
 * session's route, and a re-entrant resume of this target would snapshot it as
 * this session's local pending turn (`resumeStartMessages`).
 */
export interface LinkedResumeEntry {
  /** Whether the warm cache already holds native (`codex:`) rows. */
  cachedNative: boolean
  /** Whether the foreground runtime binding must be released. */
  detachPrevious: boolean
  /** Whether the painted transcript belongs to another conversation. */
  clearMessages: boolean
  /** The list snapshot's own `codex_linked` marker, if there was a row. */
  listedLinked: boolean
  /** Whether this row may be a linked pointer. */
  mayBeLinked: boolean
}

export function linkedResumeEntry(state: {
  activeRuntimeId: null | string
  cachedNative: boolean
  listed: { codex_linked?: boolean } | undefined
  resumedSameSelectedSession: boolean
  warmAtEntry: null | { runtimeId: string; state: ClientSessionState }
}): LinkedResumeEntry {
  const mayBeLinked = Boolean(!state.listed || state.listed.codex_linked || state.cachedNative)
  const foregroundIsTarget = state.warmAtEntry !== null && state.warmAtEntry.runtimeId === state.activeRuntimeId

  return {
    cachedNative: state.cachedNative,
    detachPrevious: !foregroundIsTarget || mayBeLinked,
    // A same-session re-resume keeps its transcript; everything else — a
    // switch, or any linked-ish entry — starts from a clean view.
    clearMessages: !state.resumedSameSelectedSession || mayBeLinked,
    listedLinked: Boolean(state.listed?.codex_linked),
    mayBeLinked
  }
}
