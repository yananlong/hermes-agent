import { requestGatewayForAgent } from '@/store/gateway'
import type { SessionOwnerRoute } from '@/store/session-request-router'

export interface ForeignSession {
  id: string
  source: 'claude' | 'codex'
  label: string
  title: string
  cwd: string | null
  mtime: number
  turn_count: number
  excerpt: string
}

export interface ForeignPage {
  sessions: ForeignSession[]
  next_offset: number | null
  host: string
  unreadable: number
}

export interface ForeignPreview {
  truncated: boolean
  messages: { role: string; content: string }[]
  total: number
  already_imported: string | null
  cwd: string | null
}

export interface ForeignImportResult {
  session_id: string
  already_imported: boolean
}

export interface CodexThread {
  id: string
  title: string
  cwd: string | null
  updated_at: number | null
  preview: string
  source: string | null
}

export interface CodexThreadPage {
  threads: CodexThread[]
  next_cursor: string | null
}

export interface CodexLinkResult {
  session_id: string
  already_linked: boolean
}

export function codexThreadRequest<T>(
  owner: SessionOwnerRoute,
  method: 'list' | 'link',
  params: Record<string, unknown>,
  signal?: AbortSignal
) {
  return requestGatewayForAgent<T>(owner.connectionId, owner.profile, `session.codex.${method}`, params, 60_000, signal)
}

export type LinkedAgent = 'claude' | 'mistral'

export type LinkedThread = CodexThread
export type LinkedThreadPage = CodexThreadPage
export type LinkedLinkResult = CodexLinkResult

export function linkedThreadRequest<T>(
  owner: SessionOwnerRoute,
  agent: LinkedAgent,
  method: 'list' | 'link',
  params: Record<string, unknown>,
  signal?: AbortSignal
) {
  return requestGatewayForAgent<T>(
    owner.connectionId,
    owner.profile,
    `session.${agent}.${method}`,
    params,
    60_000,
    signal
  )
}

export function foreignRequest<T>(
  owner: SessionOwnerRoute,
  method: 'list' | 'preview' | 'import',
  params: Record<string, unknown>,
  signal?: AbortSignal
) {
  return requestGatewayForAgent<T>(
    owner.connectionId,
    owner.profile,
    `session.foreign.${method}`,
    params,
    60_000,
    signal
  )
}

/** Which native tool owns a linked thread. Same ids the gateway uses. */
export type ForeignCapabilityAgent = 'codex' | 'claude' | 'mistral'

/** Closed set of reasons the gateway reports for a missing capability (wire contract:
 *  `ForeignCapabilityReason`). A client keys its localized copy on the code, never on the
 *  English sentence the gateway also sends. */
export type ForeignCapabilityReason =
  | 'store_missing'
  | 'store_unreadable'
  | 'profile_scope'
  | 'cli_missing'
  | 'build_unsupported'
  | 'send_read_only'

export interface ForeignStoreCapability {
  available: boolean
  readable: boolean
  reason_code: ForeignCapabilityReason | null
  reason: string | null
}

export interface ForeignCliCapability {
  available: boolean
  /** Display token; an absolute configured binary collapses to its basename. */
  command: string
  /** `~`-collapsed when the gateway resolved one. */
  path: string | null
  version: string | null
  source: 'config' | 'path'
  reason_code: ForeignCapabilityReason | null
  reason: string | null
}

export interface ForeignContinueCapability {
  /** The gateway build has an append path for this agent at all. */
  supported: boolean
  /** A send from here would append to the native thread right now. */
  writable: boolean
  reason_code: ForeignCapabilityReason | null
  reason: string | null
}

export interface ForeignAgentCapability {
  agent: ForeignCapabilityAgent
  label: string
  store: ForeignStoreCapability
  cli: ForeignCliCapability
  continue_status: ForeignContinueCapability
}

export interface ForeignStatus {
  /** Display label of the host that answered (same one `session.foreign.list` returns). */
  host: string
  agents?: ForeignAgentCapability[]
}

/** `session.foreign.status` — host capability for the linked views, answered by the backend the
 *  client is CONNECTED to (list/link/history all run there; nothing is probed locally). */
export function foreignStatusRequest<T>(owner: SessionOwnerRoute, signal?: AbortSignal) {
  return requestGatewayForAgent<T>(
    owner.connectionId,
    owner.profile,
    'session.foreign.status',
    {},
    30_000,
    signal
  )
}
