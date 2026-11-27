import type { SessionInfo, SessionResumeResult } from '@/types/hermes'

import { sessionShouldHaveTranscript } from './utils'

/**
 * Whether a resume that published nothing must latch as FAILED instead of
 * leaving a blank chat.
 *
 * "Should have a transcript" is not the cached sessions-list row alone. That
 * row is a cache of backend truth and lags the two flows that report a vanished
 * thread: after a wake/reconnect the list can still carry the respawned
 * backend's session at `message_count` 0, and a compression tip can show 0 rows
 * while the stored transcript is intact. Conditioning the latch on the list row
 * alone paints a blank thread UNLATCHED — no retry, no error, just an empty chat
 * that looks like lost history. The resume RPC is authoritative and always
 * reports the stored size (`message_count`, filled from state.db even when
 * `messages_omitted`), so it — and a non-empty REST page — are the other rungs
 * of the same ladder.
 *
 * The check runs on the PRE-recovery transcript: an orphan journal tail must not
 * mask a lost transcript (a retry that reloads real history is safer than
 * surfacing the in-flight turn alone). Recovery only ever appends, so this
 * matches the final transcript's emptiness.
 */
export function lostTranscriptLatch(state: {
  /** Whether the REST page carried any rows. */
  pageHasRows: boolean
  /** How many rows the resume ended up publishing. */
  publishedRows: number
  resumed: SessionResumeResult
  stored: SessionInfo | undefined
}): boolean {
  const saidToHaveTranscript =
    sessionShouldHaveTranscript(state.stored) || (state.resumed.message_count || 0) > 0 || state.pageHasRows

  return saidToHaveTranscript && state.publishedRows === 0
}
