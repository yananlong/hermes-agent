import type { SessionMessage, SessionMessagesResponse } from '@/types/hermes'

export interface NativeHistoryOwner {
  connectionId: null | string
  profile: string
}

interface NativeTranscriptResult {
  linked_codex?: boolean
  linked_history_identity: string
  next_cursor: string | null
  reset: boolean
  rows: SessionMessage[]
  session_id: string
}

export class LinkedHistoryChangedError extends Error {
  constructor() {
    super('Linked session history changed while opening the conversation')
  }
}

/** Read only the authorized owner's projected native history, never its raw trace. */
export async function readNativeTranscriptPage(
  sessionId: string,
  owner: NativeHistoryOwner,
  cursor?: string,
  passive = false
): Promise<SessionMessagesResponse> {
  const { gatewayRouteState, requestGatewayForAgent } = await import('@/store/gateway')
  const params = { session_id: sessionId, profile: owner.profile, order: 'latest', limit: 100, ...(cursor ? { cursor } : {}) }
  const gateway = passive ? gatewayRouteState(owner.connectionId, owner.profile).gateway : null
  if (passive && !gateway) {
    throw new Error('The session gateway is not connected')
  }
  const result = gateway
    ? await gateway.request<NativeTranscriptResult>('session.transcript', params)
    : await requestGatewayForAgent<NativeTranscriptResult>(owner.connectionId, owner.profile, 'session.transcript', params)
  if (
    result.linked_codex !== true || result.session_id !== sessionId || !result.linked_history_identity ||
    typeof result.linked_history_identity !== 'string' || !Array.isArray(result.rows) ||
    !(result.next_cursor === null || typeof result.next_cursor === 'string') || typeof result.reset !== 'boolean' ||
    result.rows.some(row => !row || typeof row.codex_native_row_id !== 'string' || !row.codex_native_row_id)
  ) {
    throw new Error('The linked session returned invalid history')
  }
  return {
    session_id: sessionId,
    profile: owner.profile,
    // A hidden row is not a display row. Dropping it before hydration also
    // prevents hidden reasoning or tool sidecars from reaching the renderer.
    messages: result.rows.filter(row => row.display_kind !== 'hidden'),
    native_history: { identity: result.linked_history_identity, next_cursor: result.next_cursor, reset: result.reset },
    pagination: { limit: 100, offset: 0, order: 'latest', returned: result.rows.length }
  }
}
