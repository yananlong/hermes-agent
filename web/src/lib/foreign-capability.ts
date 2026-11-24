/**
 * Host capability for linked foreign sessions, as answered by the gateway the
 * dashboard is served from (`session.foreign.status`).
 *
 * The web dashboard never touches a native store itself: every linked thread on
 * this page lives on the gateway host, so "is Claude Code installed" is a
 * question for that host. This module holds the pure half (reason codes,
 * per-agent verdicts, a negative cache for gateways that predate the method) so
 * any surface with an RPC channel can render the same answer the Desktop does.
 *
 * Version skew: a gateway without the method answers -32601. That is `unknown`,
 * never "writable", and the read-only linked copy stays exactly as it was.
 */
import { JSON_RPC_METHOD_NOT_FOUND } from '@hermes/shared'

/** The native tools a Hermes row can be linked to (wire: `ForeignAgent`). */
export type ForeignCapabilityAgent = 'codex' | 'claude' | 'mistral'

/** Closed reason set the gateway reports (wire: `ForeignCapabilityReason`). */
export type ForeignCapabilityReason =
  | 'store_missing'
  | 'store_unreadable'
  | 'profile_scope'
  | 'cli_missing'
  | 'build_unsupported'
  | 'send_read_only'

export interface ForeignAgentCapability {
  agent: ForeignCapabilityAgent
  label: string
  store: {
    available: boolean
    readable: boolean
    reason_code: ForeignCapabilityReason | null
    reason: string | null
  }
  cli: {
    available: boolean
    command: string
    path: string | null
    version: string | null
    source: 'config' | 'path'
    reason_code: ForeignCapabilityReason | null
    reason: string | null
  }
  continue_status: {
    supported: boolean
    writable: boolean
    reason_code: ForeignCapabilityReason | null
    reason: string | null
  }
}

export interface ForeignStatus {
  host: string
  agents?: ForeignAgentCapability[]
}

export type HostCapability =
  | { state: 'ready'; status: ForeignStatus }
  /** The gateway predates `session.foreign.status` (or the cached verdict says so). */
  | { state: 'unknown' }
  /** The probe failed (socket, timeout). Retryable, and not cached. */
  | { state: 'error'; message: string }

/** Minimal RPC surface this module needs: `gateway.request(method, params)`. */
export type CapabilityRequester = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

const CAPABILITY_METHOD = 'session.foreign.status'

/** Serving scopes whose gateway answered -32601; module-level so every linked
 *  surface on the page shares the verdict. */
const UNSUPPORTED_SCOPES = new Set<string>()

export function resetHostCapabilityCache(): void {
  UNSUPPORTED_SCOPES.clear()
}

/** True when the failure means "this backend has no such method". */
export function isMissingCapabilityMethod(error: unknown): boolean {
  const code = error && typeof error === 'object' ? (error as { code?: unknown }).code : undefined

  if (typeof code === 'number') {
    return code === JSON_RPC_METHOD_NOT_FOUND
  }

  const message = error instanceof Error ? error.message : String(error)

  return /method not found|-32601|unknown method|no such method/i.test(message)
}

export async function readHostCapability(
  request: CapabilityRequester,
  scope = 'default'
): Promise<HostCapability> {
  if (UNSUPPORTED_SCOPES.has(scope)) {
    return { state: 'unknown' }
  }

  try {
    return { state: 'ready', status: await request<ForeignStatus>(CAPABILITY_METHOD, {}) }
  } catch (cause) {
    if (isMissingCapabilityMethod(cause)) {
      UNSUPPORTED_SCOPES.add(scope)

      return { state: 'unknown' }
    }

    return { state: 'error', message: cause instanceof Error ? cause.message : String(cause) }
  }
}

/** Product name for a linked agent (never translated). */
export function agentDisplayName(agent: ForeignCapabilityAgent): string {
  return agent === 'codex' ? 'Codex' : agent === 'mistral' ? 'Mistral Vibe' : 'Claude Code'
}

export function agentHostCapability(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): ForeignAgentCapability | null {
  if (capability?.state !== 'ready') {
    return null
  }

  return capability.status.agents?.find(row => row.agent === agent) ?? null
}

/** Why this host cannot serve the agent's store at all, or null when it can.
 *  `unknown` blocks nothing: the dashboard keeps its existing copy. */
export function storeBlockedReason(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): ForeignCapabilityReason | null {
  const row = agentHostCapability(capability, agent)

  if (!row || row.store.readable) {
    return null
  }

  return row.store.reason_code ?? 'store_missing'
}

/** True only when the host explicitly says a send would append there. */
export function isHostWritable(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): boolean {
  return agentHostCapability(capability, agent)?.continue_status.writable === true
}

/** Display label of the host that answered, when one did. */
export function capabilityHost(capability: HostCapability | undefined): string | null {
  return capability?.state === 'ready' ? capability.status.host : null
}
