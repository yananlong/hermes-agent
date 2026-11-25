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

import { dashboardConnectionGeneration } from './gateway-connection-generation'

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

/** Minimal RPC surface this module needs: `gateway.request(method, params)`.
 *
 *  Deliberately not generic. The channel is untyped JSON-RPC, and a `<T>` in the
 *  parameter position forces every *implementation* to be generic too, so an
 *  ordinary stub returning a concrete `Promise<ForeignStatus>` does not
 *  type-check and callers get pushed into `as T` lies. `readHostCapability` is
 *  the one place that knows the wire shape, and it asserts it there. A generic
 *  `request<T>()` still satisfies this narrower signature.
 */
export type CapabilityRequester = (
  method: string,
  params?: Record<string, unknown>
) => Promise<unknown>

const CAPABILITY_METHOD = 'session.foreign.status'

/** Profiles whose gateway answered -32601; module-level so every linked
 *  surface on the page shares the verdict. Cleared whenever a dashboard socket
 *  opens (see `gateway-connection-generation`). */
const UNSUPPORTED_PROFILES = new Set<string>()

/** The connection generation the cached verdicts belong to. A `-32601` is a
 *  statement about one gateway build, so the cache may not outlive the socket
 *  that produced it: the first read after a reconnect starts empty. */
let cacheGeneration = -1

function unsupportedProfiles(): Set<string> {
  const generation = dashboardConnectionGeneration()

  if (generation !== cacheGeneration) {
    UNSUPPORTED_PROFILES.clear()
    cacheGeneration = generation
  }

  return UNSUPPORTED_PROFILES
}

export function resetHostCapabilityCache(): void {
  UNSUPPORTED_PROFILES.clear()
  cacheGeneration = dashboardConnectionGeneration()
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

/** Profiles that mean "the serving process's own profile": `""` is how the
 *  dashboard's profile scope spells it, and `default` is the reserved alias the
 *  gateway resolves to the same home. Neither is sent as a `profile` param. */
const LAUNCH_PROFILES = new Set(['', 'default'])

/** Params for one capability read of `profile`.
 *
 *  `session.foreign.status` is `@_profile_scoped` and resolves the scope from
 *  `params['profile']`, so the profile the view is showing has to travel with
 *  the call: asking with `{}` answers for the launch profile and renders
 *  another profile's verdict on screen. */
function capabilityParams(profile: string): Record<string, unknown> {
  return LAUNCH_PROFILES.has(profile) ? {} : { profile }
}

/**
 * Ask the gateway that serves this dashboard what it can do for linked threads.
 *
 * `profile` is the profile whose answer we want — it is BOTH the parameter sent
 * to the profile-scoped RPC and the key the -32601 verdict is cached under (the
 * dashboard holds one gateway connection, so the profile is the whole scope).
 * Never pass a composite scope key here; the wire param must be a bare profile.
 */
export async function readHostCapability(
  request: CapabilityRequester,
  profile = 'default'
): Promise<HostCapability> {
  if (unsupportedProfiles().has(profile)) {
    return { state: 'unknown' }
  }

  try {
    const status = (await request(CAPABILITY_METHOD, capabilityParams(profile))) as ForeignStatus

    return { state: 'ready', status }
  } catch (cause) {
    if (isMissingCapabilityMethod(cause)) {
      unsupportedProfiles().add(profile)

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
