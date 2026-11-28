/**
 * Shared vocabulary for `/link-thread` — the linked-session feature's client side.
 *
 * A linked session is a pointer row: the native tool (Codex, Claude Code, Mistral Vibe) owns the
 * transcript on the GATEWAY host, and `session.<agent>.{list,link}` are the RPCs that enumerate
 * that store and create the pointer. The overlay (`components/linkedThreadPicker.tsx`) and the
 * command (`app/slash/commands/link.ts`) both read this module so they cannot disagree about a
 * method name, an id parameter, or what the host's capability answer means.
 */
import type { ForeignAgent, ForeignAgentCapability, SessionForeignStatusResult } from '@hermes/shared/gateway-events'

/** Native tools a Hermes row can be linked to, in the order every linked surface lists them. */
export const LINK_AGENTS = ['codex', 'claude', 'mistral'] as const satisfies readonly ForeignAgent[]

/** Product names. The gateway sends the same strings as `label`; a client needs them before it answers. */
export const LINK_AGENT_LABELS: Record<ForeignAgent, string> = {
  claude: 'Claude Code',
  codex: 'Codex',
  mistral: 'Mistral Vibe'
}

/** One agent's verdict for THIS host, as the client renders it. */
export interface LinkAgentState {
  agent: ForeignAgent
  label: string
  /** '' when this host can serve a linked thread for the agent, else the one-line reason why not. */
  reason: string
  /** Only an explicit `continue_status.writable` from the host counts as writable. */
  writable: boolean
}

/** The part of a native thread row every agent's list RPC returns (all three use the same shape). */
export interface LinkThreadRow {
  id: string
  title: string
  updated_at?: null | number
}

/** `session.<agent>.list` result: one page of rows plus the cursor for the next one. */
export interface LinkThreadPage {
  next_cursor?: null | string
  threads?: LinkThreadRow[]
}

/**
 * `session.<agent>.link` result: the Hermes pointer row the backend created (or found).
 * All three agents answer the same shape (`SessionCodexLinkResult` / `SessionClaudeLinkResult` /
 * `SessionMistralLinkResult` in the generated contract).
 */
export interface LinkCreated {
  already_linked: boolean
  session_id: string
}

const isLinkAgent = (value: string): value is ForeignAgent => (LINK_AGENTS as readonly string[]).includes(value)

/**
 * `/link-thread`'s argument split into the agent it names (when it names one) and the native thread id.
 *
 * `/link-thread codex 0a1b` names both; `/link-thread 0a1b` names only a thread, and which agent
 * owns it is a capability question the caller resolves (never a guess here). `/link-thread` and
 * `/link-thread codex` carry
 * no thread id: they open the picker.
 */
export const parseLinkArg = (arg: string): { agent: ForeignAgent | null; threadId: string } => {
  const [first = '', second = ''] = arg.trim().split(/\s+/).filter(Boolean)

  if (first && isLinkAgent(first.toLowerCase())) {
    return { agent: first.toLowerCase() as ForeignAgent, threadId: second }
  }

  return { agent: null, threadId: first }
}

/** `session.codex.list` / `session.claude.list` / `session.mistral.list`. */
export const linkListMethod = (agent: ForeignAgent): string => `session.${agent}.list`

/** `session.codex.link` / `session.claude.link` / `session.mistral.link`. */
export const linkMethod = (agent: ForeignAgent): string => `session.${agent}.link`

/**
 * The id parameter each link RPC documents: Codex threads by `thread_id`, the Claude/Vibe
 * siblings by `session_id` (their handlers accept `thread_id` as an alias, Codex does not).
 */
export const linkParams = (agent: ForeignAgent, threadId: string): Record<string, string> =>
  agent === 'codex' ? { thread_id: threadId } : { session_id: threadId }

const unavailableReason = (row: undefined | ForeignAgentCapability, label: string): string =>
  row?.continue_status?.reason ||
  row?.store?.reason ||
  row?.cli?.reason ||
  (row ? `This host cannot continue ${label} threads.` : `This gateway did not report ${label} capability.`)

/**
 * `session.foreign.status` → one row per agent, and the only place "can this host serve it" is
 * decided. An agent counts as linkable only when the host explicitly says a send would append to
 * its native threads (`continue_status.writable`): linking a thread the gateway then refuses to
 * continue would fail on submit instead of here, which is exactly what the reason line prevents.
 * No answer, a failed probe, or a gateway that names no row for the agent is UNAVAILABLE — never
 * optimistically writable.
 */
export const agentLinkStates = (status: null | SessionForeignStatusResult | undefined): LinkAgentState[] =>
  LINK_AGENTS.map(agent => {
    const label = LINK_AGENT_LABELS[agent]
    const row = status?.agents?.find(candidate => candidate.agent === agent)

    if (row?.continue_status?.writable === true) {
      return { agent, label, reason: '', writable: true }
    }

    return { agent, label, reason: unavailableReason(row, label), writable: false }
  })
