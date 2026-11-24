/**
 * Linked foreign sessions — pointer rows whose transcript is owned by a
 * native tool (Codex app-server, Claude Code, Mistral Vibe) instead of a
 * Hermes state.db. The session list flags them (`codex_linked`,
 * `claude_linked`, `mistral_linked`); every consumer that special-cases one
 * agent must treat all three identically, so they funnel through here.
 */
import type { SessionInfo } from '@/hermes'

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
