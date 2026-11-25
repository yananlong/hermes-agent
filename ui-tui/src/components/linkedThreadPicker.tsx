import { Box, Text, useInput, useStdout } from '@hermes/ink'
import type { ForeignAgent, SessionForeignStatusResult } from '@hermes/shared/gateway-events'
import { useEffect, useMemo, useRef, useState } from 'react'

import {
  agentLinkStates,
  type LinkAgentState,
  type LinkCreated,
  linkListMethod,
  linkMethod,
  linkParams,
  type LinkThreadPage,
  type LinkThreadRow
} from '../app/slash/linkedThreads.js'
import type { GatewayClient } from '../gatewayClient.js'
import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

import { OverlayHint, windowItems } from './overlayControls.js'
import { chipRowProps, clampOverlayWidth } from './overlayPrimitives.js'

const VISIBLE = 10
const MIN_WIDTH = 52
const MAX_WIDTH = 100

/** One selectable native thread, with the agent whose store it came from. */
interface PickerRow {
  agent: ForeignAgent
  index: number
  thread: LinkThreadRow
}

/** One agent's section: its verdict, its threads, and the reason it has none. */
interface PickerSection extends LinkAgentState {
  loaded: boolean
  threads: LinkThreadRow[]
}

/** Short, copy-friendly id: native ids are UUIDs (Codex, Claude Code) or session dir names (Vibe). */
const shortId = (id: string) => (id.length > 12 ? id.slice(0, 12) : id)

const rowTitle = (thread: LinkThreadRow) => thread.title.trim() || thread.id

const rowKey = (agent: ForeignAgent, id: string) => `${agent}:${id}`

/**
 * `/link-thread`'s picker: the native threads this host can be continued from, one section per agent.
 *
 * The capability answer (`session.foreign.status`) is asked ONCE when the panel opens and decides
 * what is selectable: an agent the host cannot serve renders its reason and no rows, so Enter can
 * never fire a link the next send would refuse. Threads come from the same RPCs the linked
 * transcript views read (`session.<agent>.list`), and a row links + resumes through `onResume`, so
 * the user lands in the new Hermes session with the prompt ready.
 */
export function LinkedThreadPicker({ gw, initialAgent, maxWidth, onCancel, onResume, t }: LinkedThreadPickerProps) {
  const [statusHost, setStatusHost] = useState('')
  const [sections, setSections] = useState<null | PickerSection[]>(null)
  const [idx, setIdx] = useState(0)
  const [linking, setLinking] = useState(false)
  const [err, setErr] = useState('')
  const scoped = useRef(false)

  const { stdout } = useStdout()
  // Pinned width so FloatBox's shrink-to-fit parent doesn't resize as titles scroll in, and
  // `truncate-end` on each row has a real constraint. Optional maxWidth hands a grid cell its budget.
  const preferredWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6))
  const width = clampOverlayWidth(preferredWidth, maxWidth)

  useEffect(() => {
    let live = true

    // One page per agent the host can serve; a failing store blocks only that agent.
    const loadThreads = async (next: PickerSection[]) => {
      const loaded = await Promise.all(
        next.map(async section => {
          if (!section.writable) {
            return { ...section, loaded: true }
          }

          try {
            const page = asRpcResult<LinkThreadPage>(await gw.request(linkListMethod(section.agent), {}))

            return { ...section, loaded: true, threads: page?.threads ?? [] }
          } catch (e: unknown) {
            // The list is the store probe that matters for a link: no page, no rows, host's reason.
            return { ...section, loaded: true, reason: rpcErrorMessage(e), threads: [], writable: false }
          }
        })
      )

      if (live) {
        setSections(loaded)
      }
    }

    gw.request<SessionForeignStatusResult>('session.foreign.status', {})
      .then(raw => {
        if (!live) {
          return
        }

        const status = asRpcResult<SessionForeignStatusResult>(raw)

        if (!status) {
          setErr('error: invalid response: session.foreign.status')

          return
        }

        const next = agentLinkStates(status).map(state => ({ ...state, loaded: false, threads: [] }))

        setStatusHost(status.host)
        setSections(next)
        void loadThreads(next)
      })
      .catch((e: unknown) => {
        if (!live) {
          return
        }

        // No capability answer is NOT writable: every agent stays unavailable with its reason.
        setErr(`error: ${rpcErrorMessage(e)}`)
        setSections(agentLinkStates(null).map(state => ({ ...state, loaded: true, threads: [] })))
      })

    return () => {
      live = false
    }
  }, [gw])

  const rows = useMemo(
    () =>
      (sections ?? []).reduce<PickerRow[]>(
        (acc, section) =>
          section.writable
            ? [...acc, ...section.threads.map(thread => ({ agent: section.agent, index: acc.length, thread }))]
            : acc,
        []
      ),
    [sections]
  )

  // Scope to the agent the command named: land on its first thread once its page arrives.
  useEffect(() => {
    if (scoped.current || !initialAgent || rows.length === 0) {
      return
    }

    scoped.current = true

    const first = rows.findIndex(row => row.agent === initialAgent)

    if (first >= 0) {
      setIdx(first)
    }
  }, [initialAgent, rows])

  useEffect(() => {
    if (idx > rows.length - 1) {
      setIdx(Math.max(0, rows.length - 1))
    }
  }, [idx, rows.length])

  const link = (row: PickerRow) => {
    setLinking(true)
    setErr('')

    gw.request<LinkCreated>(linkMethod(row.agent), linkParams(row.agent, row.thread.id))
      .then(raw => {
        const created = asRpcResult<LinkCreated>(raw)

        if (!created?.session_id) {
          setErr(`error: invalid response: ${linkMethod(row.agent)}`)
          setLinking(false)

          return
        }

        onResume(created.session_id)
      })
      .catch((e: unknown) => {
        setErr(`error: ${rpcErrorMessage(e)}`)
        setLinking(false)
      })
  }

  useInput((input, key) => {
    if (linking) {
      return
    }

    if (key.escape || input === 'q') {
      return onCancel()
    }

    if (key.upArrow) {
      return setIdx(current => Math.max(0, current - 1))
    }

    if (key.downArrow) {
      return setIdx(current => Math.min(rows.length - 1, current + 1))
    }

    if (key.return) {
      const row = rows[idx]

      if (row) {
        link(row)
      }
    }
  })

  const { items, offset } = windowItems(rows, idx, VISIBLE)
  const visible = new Set(items.map(row => rowKey(row.agent, row.thread.id)))

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent} wrap="truncate-end">
        Link a native thread
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        {statusHost
          ? `continue a Codex / Claude Code / Mistral Vibe thread · host: ${statusHost}`
          : 'continue a Codex / Claude Code / Mistral Vibe thread'}
      </Text>
      <Text color={t.color.label} wrap="truncate-end">
        {err || linking ? err || 'linking…' : ' '}
      </Text>

      {sections === null ? (
        <Text color={t.color.muted}>checking what this host can serve…</Text>
      ) : (
        sections.map(section => (
          <Box flexDirection="column" key={section.agent}>
            <Text bold color={t.color.muted} wrap="truncate-end">
              {section.label}
            </Text>
            {section.writable
              ? section.threads.map(thread => {
                  const index = rows.find(row => row.agent === section.agent && row.thread.id === thread.id)?.index

                  const shown = visible.has(rowKey(section.agent, thread.id))
                  const active = index !== undefined && index === idx

                  return shown ? (
                    <Text
                      color={active ? t.color.text : t.color.muted}
                      {...chipRowProps(t, active)}
                      key={rowKey(section.agent, thread.id)}
                      wrap="truncate-end"
                    >
                      {active ? '▸ ' : '  '}
                      {rowTitle(thread)} · {shortId(thread.id)}
                    </Text>
                  ) : null
                })
              : null}
            {section.writable && section.threads.length === 0 ? (
              <Text color={t.color.muted} wrap="truncate-end">
                {section.loaded ? '  no threads in its store' : '  loading threads…'}
              </Text>
            ) : null}
            {section.writable ? null : (
              <Text color={t.color.label} wrap="truncate-end">
                {'✗ '}
                {section.reason}
              </Text>
            )}
          </Box>
        ))
      )}

      {offset > 0 ? (
        <Text color={t.color.muted} wrap="truncate-end">
          ↑ {offset} more above
        </Text>
      ) : null}
      {offset + VISIBLE < rows.length ? (
        <Text color={t.color.muted} wrap="truncate-end">
          ↓ {rows.length - offset - VISIBLE} more below
        </Text>
      ) : null}

      <OverlayHint t={t}>{'↑↓ move · Enter link · Esc close'}</OverlayHint>
    </Box>
  )
}

interface LinkedThreadPickerProps {
  gw: GatewayClient
  /** `/link-thread <agent>`: select that agent's first thread when its page arrives. */
  initialAgent?: ForeignAgent
  maxWidth?: number
  onCancel: () => void
  /** Resume the Hermes session a link returned (`actions.resumeById`). */
  onResume: (sessionId: string) => void
  t: Theme
}
