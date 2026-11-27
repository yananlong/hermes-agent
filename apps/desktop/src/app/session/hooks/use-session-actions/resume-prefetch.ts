import { LinkedHistoryChangedError } from '@/api/native-session-history'
import type { SessionMessagesResponse, SessionResumeResult } from '@/types/hermes'

/**
 * Settlement of the concurrent REST prefetch and `session.resume` attach.
 *
 * The two run in parallel so a big session's wall time is `max(read, attach)`
 * instead of their sum, but only the attach establishes a linked pointer's
 * native identity. That makes the read provisional until the attach lands:
 *
 * - The pointer may have become linked while an ordinary REST read was in
 *   flight. The attach decides; the caller drops what it had painted.
 * - A linked pointer can be retargeted between the read and the attach, so the
 *   page is published only when the owner confirms the same native thread. A
 *   mismatch is a changed history, never a graft.
 * - The socket can drop and redial while the read is in flight. Publishing
 *   then would let the new socket's replay append the same turn twice, so a
 *   failed replay barrier drops the page.
 */
export interface PrefetchAgainstAttach {
  /** The attach snapshot, already awaited. */
  attached: SessionResumeResult
  /** True when the attach revealed a linked pointer the caller had not seen. */
  becameLinked: boolean
  /** The publishable page, or null when it was never read or was dropped. */
  page: null | SessionMessagesResponse
  /** The read's own failure, for a linked pointer that ends up with no page. */
  readError: unknown
}

export async function prefetchAgainstAttach(options: {
  /** Whether the row was known to be a linked pointer before the attach. */
  linkedCodex: boolean
  /** The concurrent REST read, or null when this resume skips the prefetch. */
  prefetch: null | Promise<SessionMessagesResponse>
  /** Re-read after the attach, when it revealed a linked pointer. */
  readAgain: () => Promise<SessionMessagesResponse>
  /** The pending attach; awaited here so both settle before anything publishes. */
  resume: Promise<SessionResumeResult>
  /** Resolves false when the redialed socket dropped this read. */
  replayBarrier: () => Promise<boolean> | undefined
}): Promise<PrefetchAgainstAttach> {
  let page: null | SessionMessagesResponse = null
  let readError: unknown = null

  try {
    if (options.prefetch) {
      page = await options.prefetch
    }
  } catch (error) {
    readError = error
  }

  const attached = await options.resume
  const linkedIdentity = attached.linked_history_identity
  // The pointer turned linked only on this attach: the caller had read it as an
  // ordinary session, so whatever it painted is not this thread's transcript.
  const becameLinked = Boolean(linkedIdentity) && !options.linkedCodex

  if (linkedIdentity && !page) {
    page = await options.readAgain()
  }

  // A linked pointer with neither a page nor a live projection is a failed
  // native read, and must not resume into an empty transcript silently.
  if (options.linkedCodex && !page && readError) {
    throw readError
  }

  const replay = options.replayBarrier()

  if (page && replay && !(await replay)) {
    page = null
  }

  if (page && (linkedIdentity || page.native_history) && linkedIdentity !== page.native_history?.identity) {
    throw new LinkedHistoryChangedError()
  }

  return { attached, becameLinked, page, readError }
}
