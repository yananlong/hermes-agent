// Linked foreign session (Codex / Claude / Mistral pointer row) surface state for the
// chat view: which agent the viewed row links to, whether a send can append to the
// native thread on the serving host, and the read-only note this renders.
// Extracted from ``app/chat/index.tsx`` for its complexity ratchet; the derivation
// contract is unchanged.

import { useMemo } from 'react'

import { useI18n } from '@/i18n'
import { linkedForeignAgent } from '@/lib/linked-session'
import { useStoreSelector } from '@/lib/use-session-slice'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'

import {
  agentDisplayName,
  continueReason,
  isLinkedThreadWritable
} from '../session-import/capability'
import { useHostCapability } from '../session-import/use-host-capability'

export interface LinkedSessionSurface {
  /** The linked foreign agent of the selected/resumed row, or null for an ordinary session. */
  agent: 'claude' | 'codex' | 'mistral' | null
  /** True only when the serving host confirmed a send would append to the native thread. */
  appendable: boolean
  /** The capability answer to show for a linked row (null when not linked). */
  readOnlyReason: ReturnType<typeof continueReason>
}

/**
 * The linked-session verdict for the chat surface: the selected row's linked agent,
 * whether the connected gateway can append a turn to the native thread there, and the
 * reason text when it cannot. ``appendable`` is false on every unknown answer —
 * read-only is the fallback, exactly as before this hook existed.
 */
export function useLinkedSessionSurface(
  selectedSessionId: null | string,
  activeSessionId: null | string,
  composerScope: { connectionId: null | string; profile: null | string },
  activeGatewayProfile: string
): LinkedSessionSurface {
  const linkedAgent = useStoreSelector($sessions, rows => {
    const id = selectedSessionId || activeSessionId

    return linkedForeignAgent(id ? rows.find(session => sessionMatchesStoredId(session, id)) : undefined)
  })

  // Can a send APPEND to that native thread? Only the CONNECTED gateway knows: it owns the
  // store and spawns the CLI. `undefined` (no owner yet) and an old gateway's -32601 both read
  // as unknown, which keeps the read-only note and the hidden composer exactly as before.
  const owner = useMemo<SessionOwnerRoute | undefined>(() => {
    const connectionId = composerScope.connectionId
    const profile = composerScope.profile ?? activeGatewayProfile

    return connectionId && profile ? { connectionId, profile } : undefined
  }, [composerScope.connectionId, composerScope.profile, activeGatewayProfile])

  const capability = useHostCapability(linkedAgent ? owner : undefined)
  const appendable = Boolean(linkedAgent && isLinkedThreadWritable(capability, linkedAgent))
  const readOnlyReason = linkedAgent ? continueReason(capability, linkedAgent) : 'capability_unknown'

  return { agent: linkedAgent, appendable, readOnlyReason }
}

/**
 * The linked-session note bar: read-only warning, or the host's writable/appendable
 * confirmation. Null when the session is not a linked foreign row.
 */
export function linkedSessionNote(
  t: ReturnType<typeof useI18n>['t'],
  surface: LinkedSessionSurface
): null | { appendable: boolean; reason: string; text: string } {
  const linkedAgent = surface.agent
  if (!linkedAgent) {
    return null
  }
  if (surface.appendable) {
    return {
      appendable: true,
      reason: surface.readOnlyReason ?? 'writable',
      text: t.sessionImport.hostCapabilityWritable(agentDisplayName(linkedAgent))
    }
  }
  const { readOnlyReason } = surface
  if (readOnlyReason && readOnlyReason !== 'capability_unknown') {
    return {
      appendable: false,
      reason: readOnlyReason,
      text: t.sessionImport.hostCapabilityReadOnly(agentDisplayName(linkedAgent))
    }
  }
  return { appendable: false, reason: readOnlyReason ?? 'capability_unknown', text: t.sessionImport.linkedReadonlyWarning }
}
