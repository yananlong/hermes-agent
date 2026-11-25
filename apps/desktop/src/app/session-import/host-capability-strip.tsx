/**
 * Per-agent host capability, as the CONNECTED gateway reported it.
 *
 * A remote Desktop cannot know whether Claude Code or the Vibe CLI exists on the machine that
 * owns the linked threads, so this strip only ever repeats what `session.foreign.status` said.
 * Before an answer arrives (or against a gateway that predates the method) it says so once instead
 * of guessing per agent: the linked views stay usable in every state.
 */
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'

import { agentDisplayName, agentHostCapability, type HostCapability } from './capability'
import { agentAvailabilityText } from './capability-copy'

const AGENTS = ['codex', 'claude', 'mistral'] as const

interface HostCapabilityStripProps {
  capability: HostCapability | undefined
  /** Host label to show before the first answer (`session.foreign.list` carries the same one). */
  fallbackHost?: string | null
  /**
   * Whose reason the caller renders itself, next to its own controls — `'all'` when the caller
   * lists every blocked agent (the picker's note block). Those agents keep only their name and
   * status icon here, so one explanation never appears twice on the same screen.
   */
  detailAgent?: (typeof AGENTS)[number] | 'all'
}

export function HostCapabilityStrip({ capability, fallbackHost, detailAgent }: HostCapabilityStripProps) {
  const { t } = useI18n()
  const copy = t.sessionImport
  const host = (capability?.state === 'ready' ? capability.status.host : fallbackHost) || copy.connectedComputer
  const ready = capability?.state === 'ready'

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-(--ui-text-secondary)" role="status">
      <span className="inline-flex items-center gap-2">
        <Codicon name="device-desktop" />
        {copy.readingFrom} {host}
      </span>
      {!ready && <span>{capability?.state === 'unknown' ? copy.hostCapabilityUnknown : capability?.state === 'error' ? copy.hostCapabilityError : copy.hostCapabilityChecking}</span>}
      {ready &&
        AGENTS.map(agent => {
          const row = agentHostCapability(capability, agent)
          const available = Boolean(row?.store.readable && row.cli.available)
          // The contextual note under the caller's own buttons already explains this agent;
          // repeating the sentence here would print the same explanation twice on one screen.
          const deferred = !available && (detailAgent === 'all' || agent === detailAgent)

          return (
            <span
              className="inline-flex items-center gap-2"
              data-agent={agent}
              data-available={available ? 'true' : 'false'}
              key={agent}
            >
              <Codicon name={available ? 'pass-filled' : 'circle-slash'} />
              {deferred ? agentDisplayName(agent) : agentAvailabilityText(copy, capability, agent, host)}
            </span>
          )
        })}
    </div>
  )
}
