import { toChatMessages } from '@/lib/chat-messages'
import type { ChatMessage } from '@/lib/chat-messages'

import {
  chatMessageArraysEquivalent,
  dedupeInflightUserAgainstTranscript,
  overlayConcurrentMessageChanges,
  preserveLocalPendingTurnMessages,
  removeRepresentedLocalLiveProjection
} from './utils'
import {
  liveTurnSettledDuringResume,
  reconcileAuthoritativeChatMessages,
  reconcileAuthoritativeMessages
} from './resume-reconcile'
import type { SessionMessagesResponse, SessionResumeResult } from '@/types/hermes'

/**
 * Which transcript a resume publishes.
 *
 * Four sources compete and only one may win: what is already on screen, the
 * persisted REST page the prefetch painted, the attach snapshot's own
 * projection, and a reply the live stream settled while the RPC was in flight
 * (the newest of all). The page is preferred over the snapshot because it is
 * the display authority and converting a 1000+-message snapshot costs over a
 * second; the snapshot only wins for a page older than itself (it lacks a
 * durable row the snapshot carries). Every branch ends on the same identity
 * check so an unchanged result keeps the current array and React skips the
 * rebuild.
 */
export function preferredResumeMessages(state: {
  currentMessages: ChatMessage[]
  hasLiveProjection: boolean
  /** The attach snapshot, already awaited. */
  resumed: SessionResumeResult
  /** The snapshot's authoritative RPC options. */
  authoritativeSnapshot: boolean | undefined
  /** Whether the prefetch page was grafted and still serves this snapshot. */
  prefetchApplies: boolean
  /** The prefetched page's own rows, for reconciliation anchors. */
  prefetchedMessages: ChatMessage[] | null
  /** The prefetched page as the backend returned it. */
  prefetchedResult: null | SessionMessagesResponse
  /** The grafted snapshot the resume started from. */
  localSnapshot: ChatMessage[]
  /** The wall clock at which the attach began, in seconds. */
  resumeStartedAt: number
  resumedSameSelectedSession: boolean
  /** Messages captured at resume entry, kept across a same-session re-resume. */
  resumeStartMessages: ChatMessage[]
}): ChatMessage[] {
  const { currentMessages } = state

  // A reply the live stream settled during the RPC is newer than this snapshot.
  if (
    (state.authoritativeSnapshot && state.resumed.messages.length === 0 && !state.hasLiveProjection) ||
    liveTurnSettledDuringResume(state.resumed, state.resumeStartedAt, state.resumedSameSelectedSession)
  ) {
    return currentMessages
  }

  if (state.prefetchApplies) {
    if (state.hasLiveProjection && state.prefetchedMessages) {
      const runtimeMessages = toChatMessages(state.resumed.messages)
      const previousMessages = removeRepresentedLocalLiveProjection(currentMessages, state.resumed)

      // Omitted-messages resumes stay safe here: `resumed.messages` is empty,
      // so `runtimeMessages` has no anchor and the dedupe helper returns the
      // projection unchanged, while the REST prefetch remains the authoritative
      // transcript — the same "graft, don't rebuild" outcome the pre-restructure
      // messages_omitted branch produced.
      const liveProjection = dedupeInflightUserAgainstTranscript(
        state.prefetchedMessages,
        runtimeMessages,
        state.resumed
      )

      const resumedMessages = reconcileAuthoritativeChatMessages(
        state.prefetchedMessages,
        previousMessages,
        liveProjection,
        state.prefetchedResult?.messages
      )

      const withConcurrentChanges = overlayConcurrentMessageChanges(
        resumedMessages,
        state.localSnapshot,
        currentMessages
      )

      return chatMessageArraysEquivalent(currentMessages, withConcurrentChanges)
        ? currentMessages
        : withConcurrentChanges
    }

    if (!state.hasLiveProjection) {
      return state.localSnapshot
    }
  }

  const previousMessages = state.resumedSameSelectedSession
    ? preserveLocalPendingTurnMessages(currentMessages, state.resumeStartMessages)
    : currentMessages

  const resumedMessages = reconcileAuthoritativeMessages(state.resumed.messages, previousMessages, state.resumed)

  return chatMessageArraysEquivalent(currentMessages, resumedMessages) ? currentMessages : resumedMessages
}
