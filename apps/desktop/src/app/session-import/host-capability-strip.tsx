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

import { agentHostCapability, type HostCapability } from './capability'
import { agentAvailabilityText } from './capability-copy'

const AGENTS = ['codex', 'claude', 'mistral'] as const

interface HostCapabilityStripProps {
  capability: HostCapability | undefined
  /** Host label to show before the first answer (`session.foreign.list` carries the same one). */
  fallbackHost?: string | null
}

export function HostCapabilityStrip({ capability, fallbackHost }: HostCapabilityStripProps) {
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

          return (
            <span
              className="inline-flex items-center gap-2"
              data-agent={agent}
              data-available={available ? 'true' : 'false'}
              key={agent}
            >
              <Codicon name={available ? 'pass-filled' : 'circle-slash'} />
              {agentAvailabilityText(copy, capability, agent, host)}
            </span>
          )
        })}
    </div>
  )
}
