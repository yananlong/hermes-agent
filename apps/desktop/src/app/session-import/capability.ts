/**
 * Host capability for linked foreign sessions — asked of the CONNECTED gateway.
 *
 * A linked thread's transcript lives in the native tool's store on the gateway host, and a
 * writable turn spawns that tool THERE. A desktop renderer therefore cannot answer "is Claude
 * Code installed" from its own machine: it asks `session.foreign.status` on the backend it is
 * routed to (the same `owner.connectionId` every other foreign RPC uses).
 *
 * ## Version skew, both directions
 *
 * Renderer and gateway ship separately: the Electron shell loads its own bundled renderer
 * (`file://…/dist/index.html`), so a new renderer routinely meets an older gateway.
 *
 * - Old gateway (method unknown → -32601): reported as `unknown`, never as "writable". The
 *   negative result is cached per serving scope so a linked view opened twice does not dial for a
 *   method this backend will never have. `unknown` keeps the pre-capability behaviour: transcripts
 *   still open, a link is still offered (the list RPC already proved the store readable), and the
 *   read-only linked note stays.
 * - New gateway, old renderer: nothing to do here — the gateway's linked list/history/link RPCs
 *   are unchanged, and a send the old UI never offers is not needed to view a transcript.
 *
 * A capability answer NEVER gates opening a transcript: `blockedReason` only ever disables an
 * affordance whose RPC the gateway has already told us cannot work here.
 */
import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import {
  isHostCapabilityUnsupported,
  markHostCapabilityUnsupported
} from '@/store/host-capability-cache'
import type { SessionOwnerRoute } from '@/store/session-request-router'

import {
  type ForeignAgentCapability,
  type ForeignCapabilityAgent,
  type ForeignCapabilityReason,
  type ForeignStatus,
  foreignStatusRequest
} from './api'

export type HostCapability =
  | { state: 'ready'; status: ForeignStatus }
  /** The backend predates `session.foreign.status` (or the cached verdict says it does). */
  | { state: 'unknown' }
  /** The probe itself failed (offline, timeout). Same behaviour as `unknown`, but retryable. */
  | { state: 'error'; message: string }

/** Serving scopes whose gateway answered -32601 for the capability method. Module-level so every
 *  linked view in the window shares the verdict; `store/gateway.ts` clears it whenever a socket
 *  opens, so an upgraded gateway is asked again instead of staying "unknown" until relaunch.
 *  Re-exported here because this is the module every caller (and test) already imports. */
export { resetHostCapabilityCache } from '@/store/host-capability-cache'

/** The scope a capability answer belongs to: one gateway backend serving one profile. */
export function hostCapabilityScope(owner: SessionOwnerRoute): string {
  return `${owner.connectionId ?? 'local'}\u0000${owner.profile}`
}

export async function readHostCapability(
  owner: SessionOwnerRoute,
  signal?: AbortSignal
): Promise<HostCapability> {
  const scope = hostCapabilityScope(owner)

  if (isHostCapabilityUnsupported(scope)) {
    return { state: 'unknown' }
  }

  try {
    return { state: 'ready', status: await foreignStatusRequest<ForeignStatus>(owner, signal) }
  } catch (cause) {
    if (signal?.aborted) {
      return { state: 'unknown' }
    }

    if (isMissingRpcMethod(cause)) {
      markHostCapabilityUnsupported(scope)

      return { state: 'unknown' }
    }

    return { state: 'error', message: cause instanceof Error ? cause.message : String(cause) }
  }
}

/** Product names: the gateway's `label` is the same string, but a linked row can render before
 *  any capability answer arrives. */
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

/** Why the "open / link this thread" affordance cannot work here, or null when it can.
 *
 *  Codex links through app-server (store AND binary); the Claude/Vibe links read the store
 *  directly, so their CLI stays irrelevant until a send. `unknown` capability blocks nothing:
 *  the list that produced the row already read that same store through the same gateway. */
export function linkBlockedReason(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): ForeignCapabilityReason | null {
  const row = agentHostCapability(capability, agent)

  if (!row) {
    return null
  }

  if (!row.store.readable) {
    return row.store.reason_code ?? 'store_missing'
  }

  if (agent === 'codex' && !row.cli.available) {
    return row.cli.reason_code ?? 'cli_missing'
  }

  return null
}

/** Why a send cannot append to the native thread here, or null when it can.
 *
 *  Only `ready` + `writable` is writable. `unknown` (old gateway) and a failed probe are NOT
 *  writable: the read-only linked note stays the fallback branch, exactly as before this RPC
 *  existed, and the composer is not offered for a thread we cannot append to. */
export function continueReason(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): ForeignCapabilityReason | 'capability_unknown' | null {
  const row = agentHostCapability(capability, agent)

  // No answer, a failed probe, or a gateway that names no row for this agent: nothing is known,
  // so nothing is writable. Only an explicit `writable: true` unlocks the composer.
  if (!row) {
    return 'capability_unknown'
  }

  if (row.continue_status.writable) {
    return null
  }

  return row.continue_status.reason_code ?? 'build_unsupported'
}

/** True when the host told us a send would append to the native thread. */
export function isLinkedThreadWritable(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): boolean {
  return agentHostCapability(capability, agent)?.continue_status.writable === true
}

/** The gateway's own reason sentence for a blocked capability, when it sent one. */
export function gatewayReason(
  capability: HostCapability | undefined,
  agent: ForeignCapabilityAgent
): string | null {
  const row = agentHostCapability(capability, agent)

  return row?.continue_status.reason ?? row?.store.reason ?? null
}
