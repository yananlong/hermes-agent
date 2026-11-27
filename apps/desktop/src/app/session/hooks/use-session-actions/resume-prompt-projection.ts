import type { ChatMessage } from '@/lib/chat-messages'
import { restorePendingClarifyToolCall, settlePendingClarifyToolCall } from '@/lib/chat-messages'
import type { SessionResumeResult } from '@/types/hermes'

import { pendingClarifyToolPayload, restorePendingClarifyFromSnapshot } from './restore-pending-clarify'
import { projectPendingConnection, restorePendingConnectionFromSnapshot } from './restore-pending-connection'

/**
 * The prompt rows a resume snapshot re-publishes.
 *
 * An attach dropped every previous session's controls, so a still-pending
 * approval / clarify / connection prompt is only visible again if the snapshot
 * itself carries it. Each prompt projects onto the transcript as a synthetic
 * tool row: a clarify that the snapshot no longer lists is settled (not
 * dropped) so its question does not linger, and a connection request wins the
 * view because the user cannot answer anything else until it resolves.
 */
export interface ResumedPromptProjection {
  /** A clarify the snapshot authoritatively no longer holds. */
  clarifyAuthoritativelyAbsent: boolean
  /** The settle projection for a clarify that is gone, when it left a row. */
  clearedClarifyProjection: ReturnType<typeof settlePendingClarifyToolCall> | null
  /** The clarify prompt projected as a synthetic tool row. */
  pendingClarifyProjection: ReturnType<typeof restorePendingClarifyToolCall> | null
  /** The pending connection request, if the snapshot carried one. */
  pendingConnection: ReturnType<typeof restorePendingConnectionFromSnapshot>['request']
  /** The connection prompt projected as a synthetic tool row. */
  pendingConnectionProjection: ReturnType<typeof projectPendingConnection>
  /** The pending clarify request, if the snapshot carried one. */
  pendingClarify: ReturnType<typeof restorePendingClarifyFromSnapshot>['request']
  pendingClarifyState: ReturnType<typeof restorePendingClarifyFromSnapshot>
  /** The transcript as it will be published, prompt rows included. */
  visibleMessages: ChatMessage[]
}

export function projectResumedPrompts(state: {
  messages: ChatMessage[]
  /** Clarify rows already published by a live event, which outrank the snapshot. */
  liveClarifyRequestId: string | undefined
  resumed: SessionResumeResult
  resumedRunning: boolean
  resumeStartedAt: number
}): ResumedPromptProjection {
  const pendingClarifyState = restorePendingClarifyFromSnapshot(
    state.resumed,
    state.resumed.session_id,
    state.resumeStartedAt
  )
  const pendingClarify = pendingClarifyState.request
  const pendingConnection = restorePendingConnectionFromSnapshot(
    state.resumed,
    state.resumed.session_id,
    state.resumeStartedAt
  ).request
  const clarifyAuthoritativelyAbsent = pendingClarifyState.authoritativeAbsent && !state.liveClarifyRequestId

  const pendingClarifyProjection = pendingClarify
    ? restorePendingClarifyToolCall(state.messages, pendingClarifyToolPayload(pendingClarify))
    : null
  const clearedClarifyProjection = clarifyAuthoritativelyAbsent
    ? settlePendingClarifyToolCall(
        state.messages,
        pendingClarifyState.cleared ? pendingClarifyToolPayload(pendingClarifyState.cleared) : {},
        state.resumedRunning
      )
    : null
  const pendingConnectionProjection = projectPendingConnection(
    pendingClarifyProjection?.messages ?? clearedClarifyProjection?.messages ?? state.messages,
    pendingConnection
  )

  return {
    clarifyAuthoritativelyAbsent,
    clearedClarifyProjection,
    pendingClarify,
    pendingClarifyProjection,
    pendingClarifyState,
    pendingConnection,
    pendingConnectionProjection,
    visibleMessages:
      pendingConnectionProjection?.messages ??
      pendingClarifyProjection?.messages ??
      clearedClarifyProjection?.messages ??
      state.messages
  }
}
