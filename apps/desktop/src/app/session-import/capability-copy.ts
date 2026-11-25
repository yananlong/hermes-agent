/**
 * Localized copy for a host-capability verdict.
 *
 * The gateway sends a closed `reason_code` plus an English sentence. The client renders its own
 * catalog string from the code (so a German window stays German) and falls back to the gateway's
 * sentence only for a code this build does not know — a newer gateway, an older renderer.
 */
import type { Translations } from '@/i18n'

import type { ForeignCapabilityReason } from './api'
import type { HostCapability } from './capability'
import { agentDisplayName, agentHostCapability } from './capability'

type SessionImportCopy = Translations['sessionImport']

/** Copy for one capability state. `label` is a product name, never translated. */
export function capabilityReasonText(
  copy: SessionImportCopy,
  reason: ForeignCapabilityReason | 'capability_unknown' | null,
  label: string,
  host: string
): string {
  switch (reason) {
    case 'store_missing':
    case 'store_unreadable':
    case 'profile_scope':
      return copy.hostCapabilityStore(label, host)
    case 'cli_missing':
      return copy.hostCapabilityCli(label, host)
    case 'build_unsupported':
      return copy.hostCapabilityBuild(label)
    case 'send_read_only':
      return copy.hostCapabilityReadOnly(label)
    case 'capability_unknown':
      return copy.hostCapabilityUnknown
    default:
      return ''
  }
}

/** The host label a capability answer carries, or *fallback* while we wait for one.
 *
 *  A blocked-affordance reason is only ever built from a `ready` answer (it needs a reason
 *  code), so callers that pass `null` cannot reach the empty string in user-facing copy. */
export function capabilityHost(
  capability: HostCapability | undefined,
  fallback: string | null
): string {
  return capability?.state === 'ready' ? capability.status.host : (fallback ?? '')
}

/** One-line availability summary for a single agent in the host strip. */
export function agentAvailabilityText(
  copy: SessionImportCopy,
  capability: HostCapability | undefined,
  agent: Parameters<typeof agentHostCapability>[1],
  host: string
): string {
  const row = agentHostCapability(capability, agent)
  const label = agentDisplayName(agent)

  if (!row) {
    return capability?.state === 'ready' ? copy.hostCapabilityError : copy.hostCapabilityChecking
  }

  if (row.store.readable && row.cli.available) {
    return copy.hostCapabilityAvailable(label)
  }

  return capabilityReasonText(
    copy,
    row.store.readable ? (row.cli.reason_code ?? 'cli_missing') : (row.store.reason_code ?? 'store_missing'),
    label,
    host
  )
}
