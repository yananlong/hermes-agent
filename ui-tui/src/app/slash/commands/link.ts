import type { ForeignAgent, SessionForeignStatusResult } from '@hermes/shared/gateway-events'

import { asRpcResult } from '../../../lib/rpc.js'
import { patchOverlayState } from '../../overlayStore.js'
import {
  agentLinkStates,
  LINK_AGENT_LABELS,
  type LinkAgentState,
  type LinkCreated,
  linkMethod,
  linkParams,
  parseLinkArg
} from '../linkedThreads.js'
import type { SlashCommand, SlashRunCtx } from '../types.js'

const BUSY_ACTION = 'link a native thread'

/** Open the picker, scoped to one agent when the argument named one. */
const openPicker = (agent: ForeignAgent | null) => patchOverlayState({ link: agent ? { agent } : true })

/** The host's own sentence for an agent it cannot serve here. */
const unavailableLine = (states: LinkAgentState[], agent: ForeignAgent) =>
  `${LINK_AGENT_LABELS[agent]} is unavailable here: ${
    states.find(state => state.agent === agent)?.reason ?? 'this gateway did not report its capability'
  }`

/**
 * Ask the serving host what it can do, then act — never the other way round.
 *
 * The capability answer is asked once per invocation, the way the overlay asks it once when it
 * opens: a gateway that answered "-32601 unknown method" (an older backend) or that failed the
 * probe leaves every agent unavailable, so `/link` refuses with the reason instead of creating a
 * pointer row the next send cannot use.
 */
const withAgentStates = (ctx: SlashRunCtx, run: (states: LinkAgentState[]) => void) => {
  ctx.gateway
    .rpc<SessionForeignStatusResult>('session.foreign.status', {})
    .then(raw => {
      if (ctx.stale()) {
        return
      }

      const status = asRpcResult<SessionForeignStatusResult>(raw)

      if (!status) {
        return ctx.transcript.sys('error: invalid response: session.foreign.status')
      }

      run(agentLinkStates(status))
    })
    .catch(ctx.guardedErr)
}

/** Link one native thread, then open the Hermes session the backend created for it. */
const linkThread = (ctx: SlashRunCtx, agent: ForeignAgent, threadId: string) => {
  ctx.gateway
    .rpc<LinkCreated>(linkMethod(agent), linkParams(agent, threadId))
    .then(
      ctx.guarded<LinkCreated>(created => {
        if (!created.session_id) {
          return ctx.transcript.sys(`error: invalid response: ${linkMethod(agent)}`)
        }

        if (created.already_linked) {
          ctx.transcript.sys(`${LINK_AGENT_LABELS[agent]} thread ${threadId} was already linked — opening it`)
        }

        void ctx.session.resumeById(created.session_id)
      })
    )
    .catch(ctx.guardedErr)
}

/** `/link <thread-id>` with no agent: the id is unambiguous only when ONE agent can serve it. */
const linkByThreadId = (ctx: SlashRunCtx, states: LinkAgentState[], threadId: string) => {
  const usable = states.filter(state => state.writable)

  if (usable.length === 1 && usable[0]) {
    return linkThread(ctx, usable[0].agent, threadId)
  }

  if (usable.length === 0) {
    return ctx.transcript.sys(
      `error: no agent on this host can open a linked thread (${states
        .map(state => `${state.label}: ${state.reason}`)
        .join('; ')})`
    )
  }

  return ctx.transcript.sys(
    `error: name the agent — /link <agent> ${threadId} (this host can serve ${usable
      .map(state => state.agent)
      .join(', ')})`
  )
}

export const linkCommands: SlashCommand[] = [
  {
    help: 'link a Codex, Claude Code, or Mistral Vibe thread',
    name: 'link',
    run: (arg, ctx) => {
      const { agent, threadId } = parseLinkArg(arg)

      // Bare `/link` (or `/link <agent>`) browses: linking is a write, so it stays a second step.
      if (!threadId) {
        return openPicker(agent)
      }

      // A link ends in resuming a different session — the same switch `/sessions <id>` guards,
      // so a running turn is never filed under the row we are about to open.
      if (ctx.session.guardBusySessionSwitch(BUSY_ACTION)) {
        return
      }

      withAgentStates(ctx, states => {
        if (!agent) {
          return linkByThreadId(ctx, states, threadId)
        }

        if (!states.find(state => state.agent === agent)?.writable) {
          return ctx.transcript.sys(`error: ${unavailableLine(states, agent)}`)
        }

        return linkThread(ctx, agent, threadId)
      })
    },
    usage: '[agent] [thread-id]'
  }
]
