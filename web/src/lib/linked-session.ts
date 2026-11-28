/**
 * Linked foreign sessions — pointer rows whose transcript is owned by a
 * native tool (Codex app-server, Claude Code, Mistral Vibe) instead of a
 * Hermes state.db. The session list flags them (`codex_linked`,
 * `claude_linked`, `mistral_linked`); every consumer that special-cases one
 * agent must treat all three identically, so they funnel through here.
 * Ported from apps/desktop/src/lib/linked-session.ts.
 */
import type { SessionInfo } from './api'

export type LinkedForeignAgent = 'claude' | 'codex' | 'mistral'

type LinkedFlags = Pick<SessionInfo, 'claude_linked' | 'codex_linked' | 'mistral_linked'>

/** Which native tool owns this row's transcript, or null for an ordinary
 *  Hermes-owned session. Flags are optional so older backends that predate
 *  them simply read as unlinked. */
export function linkedForeignAgent(session: LinkedFlags | null | undefined): LinkedForeignAgent | null {
  if (!session) {
    return null
  }

  if (session.codex_linked === true) {
    return 'codex'
  }

  if (session.claude_linked === true) {
    return 'claude'
  }

  if (session.mistral_linked === true) {
    return 'mistral'
  }

  return null
}

/** True when the row is a linked foreign (read-only pointer) session. */
export function isLinkedForeignSession(session: LinkedFlags | null | undefined): boolean {
  return linkedForeignAgent(session) !== null
}

/** What one `?resume=<id>` lookup answered, keyed by the id it answered for. */
export interface LinkedResumeCheck {
  id: string
  agent: LinkedForeignAgent | null
  /** True only when the gateway answered FOR THIS ID. False means the question is open. */
  verified: boolean
}

/**
 * The linked-resume gate for `/chat?resume=<id>`, and it FAILS CLOSED: a PTY is spawned only when
 * a pending-or-answered lookup positively says this id is not a native-tool pointer.
 *
 * - a linked row is ALLOWED: the embedded TUI is a canonical client, and the gateway serves a
 *   linked pointer's transcript from the native tool (`session.transcript`, order `latest`) and
 *   appends new turns to that same native thread. The browser simply never gets a second
 *   transcript of its own — the terminal pane is the one viewer, as for any other session.
 * - a lookup that has not landed yet is blocked (the caller waits; racing an unknown row into a
 *   fresh PTY is the bug this gate exists for),
 * - a lookup that failed is blocked, because "we could not tell" is not "ordinary session".
 *
 * A definitive 404 is the caller's cue to record `verified: true` with no agent — the row does not
 * exist, so it cannot be a pointer.
 */
export function linkedResumeBlocked(check: LinkedResumeCheck | null, resumeId: string | null | undefined): boolean {
  if (!resumeId) {
    return false
  }

  // Unanswered (or answered for a different target) is still "we cannot tell yet": wait.
  if (!check || check.id !== resumeId) {
    return true
  }

  // "Could not tell" must never spawn a PTY we cannot vouch for.
  return !check.verified
}

/** True when the `?resume=` target is a linked pointer whose transcript the TUI will page. The
 *  dashboard marks the pane read-only for it (the adapter below), but it still OPENS. */
export function linkedResumeReadonly(
  check: LinkedResumeCheck | null,
  resumeId: string | null | undefined
): boolean {
  return Boolean(resumeId && check?.id === resumeId && check.verified && check.agent)
}

/**
 * What the canvas should cover for a `?resume=<id>` target.
 *
 * The gate above fails closed, but "blocked" is not one visual state: a target whose linked-flag
 * lookup has not landed yet has nothing to SAY (no agent, no verdict), so covering the screen with
 * the blocked notice renders an empty panel with a stray "New chat" button on every resume. That
 * window gets a loading state; the verdict panel is reserved for an answer we actually have.
 */
export type LinkedResumeOverlay = 'none' | 'loading' | 'blocked'

export function linkedResumeOverlay(
  check: LinkedResumeCheck | null,
  resumeId: string | null | undefined
): LinkedResumeOverlay {
  if (!resumeId) {
    return 'none'
  }

  if (!check || check.id !== resumeId) {
    return 'loading'
  }

  return linkedResumeBlocked(check, resumeId) ? 'blocked' : 'none'
}
