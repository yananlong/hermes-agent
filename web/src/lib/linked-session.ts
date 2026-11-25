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
 * - a linked row is blocked (its transcript lives in the native tool, so a terminal has nothing to
 *   attach to),
 * - a lookup that has not landed yet is blocked (the caller waits; racing a pointer row into a
 *   fresh PTY is the bug this gate exists for),
 * - a lookup that failed is blocked, because "we could not tell" is not "ordinary session".
 *
 * A definitive 404 is the caller's cue to record `verified: true` with no agent — the row does not
 * exist, so it cannot be a pointer.
 */
export function linkedResumeBlocked(check: LinkedResumeCheck | null, resumeId: string | null | undefined): boolean {
  if (!resumeId || !check || check.id !== resumeId) {
    return Boolean(resumeId)
  }

  return check.agent !== null || !check.verified
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
