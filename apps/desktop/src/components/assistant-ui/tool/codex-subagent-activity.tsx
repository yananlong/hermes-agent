'use client'

import { useStore } from '@nanostores/react'
import { type FC, useEffect, useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { TimelineTimestamp } from '@/components/assistant-ui/thread/timeline-timestamp'
import { SCAFFOLD_GLYPH_CLASS, SCAFFOLD_LABEL_CLASS, SCAFFOLD_META_CLASS } from '@/components/chat/scaffold-row'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { useI18n } from '@/i18n'
import { type ChatMessage, type ChatMessagePart, toChatMessages } from '@/lib/chat-messages'
import {
  type CodexSubagentActivity as ActivityData,
  CODEX_SUBAGENT_ACTIVITY_TOOL,
  codexSubagentActivityFromArgs
} from '@/lib/codex-subagent-activity'
import { displayModelName } from '@/lib/model-status-label'
import { cn } from '@/lib/utils'
import { $gateway } from '@/store/gateway'
import { requestForOwnedSession } from '@/store/session-states'
import type { SessionMessage } from '@/types/hermes'

const PAGE_SIZE = 100
const LIVE_REFRESH_MS = 2000
const MAX_TRACE_VALUE_CHARS = 65_536
const PRIVATE_TRACE_KEYS = /(?:encrypted|agent.?thread.?id|thread.?id|agent.?path|receiver.?thread.?ids)/i

interface TracePage {
  available?: boolean
  next_cursor?: null | string
  reset?: boolean
  rows?: unknown[]
  running?: boolean
}

interface TraceState {
  error: boolean
  loading: boolean
  rows: Record<string, unknown>[]
  unavailable: boolean
  running: boolean
}

const EMPTY_TRACE: TraceState = { error: false, loading: false, rows: [], unavailable: false, running: false }

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

async function ambientRequest<T>(
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<T> {
  const gateway = $gateway.get()

  if (!gateway) {
    throw new Error('Gateway unavailable')
  }

  return gateway.request<T>(method, params, timeoutMs, signal)
}

function sessionMessages(rows: Record<string, unknown>[]): SessionMessage[] {
  return rows.map(row => ({
    ...row,
    content: row.content ?? row.text ?? '',
    role: (row.role === 'user' || row.role === 'tool' || row.role === 'system'
      ? row.role
      : 'assistant') as SessionMessage['role']
  })) as SessionMessage[]
}

function safeTraceValue(value: unknown, depth = 0): unknown {
  if (depth > 12) {
    return '[nested detail omitted]'
  }

  if (Array.isArray(value)) {
    return value.map(item => safeTraceValue(item, depth + 1))
  }

  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !PRIVATE_TRACE_KEYS.test(key))
        .map(([key, child]) => [key, safeTraceValue(child, depth + 1)])
    )
  }

  return value
}

function traceValue(value: unknown): string {
  const safeValue = safeTraceValue(value)
  let text: string

  if (typeof safeValue === 'string') {
    const trimmed = safeValue.trim()

    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        const parsed: unknown = JSON.parse(trimmed)

        text = parsed && typeof parsed === 'object' ? JSON.stringify(safeTraceValue(parsed), null, 2) : safeValue
      } catch {
        text = safeValue
      }
    } else {
      text = safeValue
    }
  } else {
    try {
      text = JSON.stringify(safeValue, null, 2) ?? String(safeValue)
    } catch {
      text = String(safeValue)
    }
  }

  return text.length > MAX_TRACE_VALUE_CHARS ? `${text.slice(0, MAX_TRACE_VALUE_CHARS)}\n[display truncated]` : text
}

function TraceToolPart({
  onOpenNested,
  part
}: {
  onOpenNested: (activity: ActivityData) => void
  part: Extract<ChatMessagePart, { type: 'tool-call' }>
}) {
  const { t } = useI18n()
  const copy = t.assistant.tool.codexSubagentActivity

  const nestedActivity =
    part.toolName === CODEX_SUBAGENT_ACTIVITY_TOOL ? codexSubagentActivityFromArgs(part.args) : undefined

  const nestedStatus = nestedActivity?.kind ? copy.kinds[nestedActivity.kind] : copy.unknownKind

  const nestedMeta = [
    nestedActivity?.model ? displayModelName(nestedActivity.model) : '',
    nestedActivity?.reasoningEffort
  ].filter(Boolean)

  return (
    <div className="mt-2 min-w-0 rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2" data-trace-tool="">
      {nestedActivity?.agentThreadId ? (
        <button
          aria-label={copy.openTrace}
          className="flex w-full min-w-0 items-center gap-2 text-left text-sm font-medium text-(--ui-text-secondary) hover:text-(--ui-text-primary) focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => onOpenNested(nestedActivity)}
          type="button"
        >
          <span className="truncate">{copy.openTrace}</span>
          <span className={SCAFFOLD_META_CLASS}>{nestedStatus}</span>
          {nestedMeta.length > 0 && (
            <span className={cn(SCAFFOLD_META_CLASS, 'hidden sm:inline')}>{nestedMeta.join(' · ')}</span>
          )}
          <span aria-hidden className="ml-auto shrink-0 text-(--ui-text-tertiary)">
            ↗
          </span>
        </button>
      ) : (
        <div className="text-sm font-medium text-(--ui-text-secondary)">
          {part.toolName}
          {part.isError ? <span className="ml-2 text-destructive">{t.assistant.tool.statusError}</span> : null}
        </div>
      )}
      {part.args !== undefined && (
        <details className="mt-2 min-w-0">
          <summary className="cursor-pointer text-xs text-(--ui-text-tertiary)">{copy.arguments}</summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-black/5 p-2 font-mono text-xs dark:bg-white/5">
            {traceValue(part.args)}
          </pre>
        </details>
      )}
      {part.result !== undefined && part.result !== '' && (
        <details className="mt-2 min-w-0">
          <summary className="cursor-pointer text-xs text-(--ui-text-tertiary)">{copy.result}</summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-black/5 p-2 font-mono text-xs dark:bg-white/5">
            {traceValue(part.result)}
          </pre>
        </details>
      )}
    </div>
  )
}

function TraceMessage({
  message,
  onOpenNested
}: {
  message: ChatMessage
  onOpenNested: (activity: ActivityData) => void
}) {
  const { t } = useI18n()
  const copy = t.assistant.tool.codexSubagentActivity

  const role =
    message.role === 'user' ? copy.userRole : message.role === 'system' ? copy.systemRole : copy.assistantRole

  return (
    <article className="min-w-0 rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2.5" data-trace-message="">
      <h3 className="mb-2 text-xs font-semibold text-(--ui-text-secondary)">{role}</h3>
      {message.parts.map((part, index) => {
        if (part.type === 'text') {
          return <MarkdownTextContent isRunning={false} key={`${message.id}:${index}`} text={part.text} />
        }

        if (part.type === 'reasoning') {
          return (
            <details className="mb-2 rounded-md bg-black/5 px-2.5 py-2 dark:bg-white/5" key={`${message.id}:${index}`}>
              <summary className="cursor-pointer text-xs font-medium text-(--ui-text-tertiary)">
                {copy.reasoningSummary}
              </summary>
              <div className="mt-2">
                <MarkdownTextContent isRunning={false} text={part.text} />
              </div>
            </details>
          )
        }

        if (part.type === 'tool-call') {
          return <TraceToolPart key={`${message.id}:${index}`} onOpenNested={onOpenNested} part={part} />
        }

        return null
      })}
    </article>
  )
}

function traceMessages(rows: Record<string, unknown>[]): ChatMessage[] {
  return toChatMessages(sessionMessages(rows))
}

function CodexSubagentTraceDialog({
  description,
  messages,
  onBack,
  onOpenChange,
  onOpenNested,
  onRetry,
  open,
  trace,
  traceDepth
}: {
  description: string
  messages: ChatMessage[]
  onBack: () => void
  onOpenChange: (open: boolean) => void
  onOpenNested: (activity: ActivityData) => void
  onRetry: () => void
  open: boolean
  trace: TraceState
  traceDepth: number
}) {
  const { t } = useI18n()
  const copy = t.assistant.tool.codexSubagentActivity

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent
        bodyClassName="grid min-h-0 grid-rows-[auto_minmax(0,1fr)_auto] text-left"
        className="max-w-3xl"
        data-slot="codex-subagent-trace"
      >
        <DialogHeader className="pr-8">
          <DialogTitle icon={CodiconAgent}>{copy.traceTitle}</DialogTitle>
          <DialogDescription>
            {copy.traceDescription} {description}
          </DialogDescription>
        </DialogHeader>

        <div className="grid min-h-0 gap-2 overflow-y-auto overscroll-y-contain">
          {trace.loading && trace.rows.length === 0 && (
            <p className="text-sm text-(--ui-text-tertiary)">{copy.loading}</p>
          )}
          {trace.unavailable && <p className="text-sm text-(--ui-text-tertiary)">{copy.traceUnavailable}</p>}
          {trace.error && (
            <p className="text-sm text-destructive" role="alert">
              {copy.loadFailed}
            </p>
          )}
          {!trace.loading && !trace.unavailable && !trace.error && trace.rows.length === 0 && (
            <p className="text-sm text-(--ui-text-tertiary)">{trace.running ? copy.waiting : copy.empty}</p>
          )}
          {messages.map(message => (
            <TraceMessage key={message.id} message={message} onOpenNested={onOpenNested} />
          ))}
          {trace.running && trace.rows.length > 0 && (
            <p className="text-xs text-(--ui-text-tertiary)" role="status">
              {copy.updating}
            </p>
          )}
        </div>

        <DialogFooter className="flex-row justify-end">
          {traceDepth > 1 && (
            <Button onClick={onBack} type="button" variant="secondary">
              {copy.backToParent}
            </Button>
          )}
          {(trace.error || trace.unavailable) && (
            <Button onClick={onRetry} type="button" variant="secondary">
              {copy.retry}
            </Button>
          )}
          <Button onClick={() => onOpenChange(false)} type="button" variant="ghost">
            {copy.close}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export const CodexSubagentActivity: FC<{ args: unknown; timestamp?: number }> = ({ args, timestamp }) => {
  const { t } = useI18n()
  const copy = t.assistant.tool.codexSubagentActivity
  const sessionView = useSessionView()
  const storedSessionId = useStore(sessionView.$storedId)
  const runtimeSessionId = useStore(sessionView.$runtimeId)
  const [open, setOpen] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [traceStack, setTraceStack] = useState<Array<ActivityData & { agentThreadId: string }>>([])
  const [trace, setTrace] = useState<TraceState>(EMPTY_TRACE)
  const activity = useMemo(() => codexSubagentActivityFromArgs(args), [args])
  const parentSessionId = storedSessionId || runtimeSessionId
  const canReadTrace = Boolean(parentSessionId && activity.agentThreadId)
  const currentActivity = traceStack.at(-1) ?? activity
  const currentThreadId = currentActivity.agentThreadId
  const messages = useMemo(() => traceMessages(trace.rows), [trace.rows])

  useEffect(() => {
    if (!open) {
      return
    }

    if (!parentSessionId || !currentThreadId) {
      setTrace({ ...EMPTY_TRACE, unavailable: true })

      return
    }

    let cancelled = false
    let inFlight = false
    let cursor: string | undefined
    let rows: Record<string, unknown>[] = []
    let timer: number | undefined
    let requestController: AbortController | undefined

    setTrace({ ...EMPTY_TRACE, loading: true })

    const fetchPage = async () => {
      if (cancelled || inFlight || document.visibilityState === 'hidden') {
        return
      }

      inFlight = true
      const controller = new AbortController()
      requestController = controller

      try {
        const page = await requestForOwnedSession<TracePage>(
          parentSessionId,
          ambientRequest,
          'session.transcript',
          {
            session_id: parentSessionId,
            codex_subagent_thread_id: currentThreadId,
            order: 'oldest',
            limit: PAGE_SIZE,
            ...(cursor ? { cursor } : {})
          },
          30_000,
          controller.signal
        )

        if (cancelled) {
          return
        }

        if (page.available !== true) {
          setTrace({ ...EMPTY_TRACE, unavailable: true })

          return
        }

        const incoming = Array.isArray(page.rows) ? page.rows.filter(isRecord) : []
        // The transcript cursor guarantees non-overlapping pages. Keep every row as returned:
        // native tool calls often share timestamps and have no durable row_id, so guessed keys
        // can collapse distinct calls and their results.
        rows = page.reset ? incoming : [...rows, ...incoming]
        cursor = typeof page.next_cursor === 'string' && page.next_cursor ? page.next_cursor : undefined
        const running = page.running === true
        const shouldDrainPage = incoming.length > 0 && Boolean(cursor)

        setTrace({ error: false, loading: false, rows, unavailable: false, running })

        // Drain any non-empty page before stopping. The backend may cap a page below `limit`
        // at its byte budget, so row count alone cannot mean the cursor reached the end. An
        // empty page is the boundary between paging and live polling for oldest-order reads.
        if (running || shouldDrainPage) {
          timer = window.setTimeout(() => void fetchPage(), shouldDrainPage ? 0 : LIVE_REFRESH_MS)
        }
      } catch {
        if (!cancelled) {
          setTrace(previous => ({ ...previous, error: true, loading: false }))
        }
      } finally {
        inFlight = false

        if (requestController === controller) {
          requestController = undefined
        }
      }
    }

    void fetchPage()

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        void fetchPage()
      }
    }

    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      cancelled = true
      requestController?.abort()

      if (timer !== undefined) {
        window.clearTimeout(timer)
      }

      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [currentThreadId, open, parentSessionId, refreshKey])

  const status = currentActivity.kind ? copy.kinds[currentActivity.kind] : copy.unknownKind

  const meta = [
    currentActivity.model ? displayModelName(currentActivity.model) : '',
    currentActivity.reasoningEffort
  ].filter(Boolean)

  const label = canReadTrace ? copy.openTrace : copy.traceUnavailable

  const openRootTrace = () => {
    if (activity.agentThreadId) {
      setTrace({ ...EMPTY_TRACE, loading: true })
      setTraceStack([{ ...activity, agentThreadId: activity.agentThreadId }])
      setOpen(true)
    }
  }

  const openNestedTrace = (nestedActivity: ActivityData) => {
    const threadId = nestedActivity.agentThreadId

    if (!threadId || threadId === currentThreadId) {
      return
    }

    setTrace({ ...EMPTY_TRACE, loading: true })
    setTraceStack(stack => [...stack, { ...nestedActivity, agentThreadId: threadId }])
  }

  const returnToParentTrace = () => {
    setTrace({ ...EMPTY_TRACE, loading: true })
    setTraceStack(stack => stack.slice(0, -1))
  }

  return (
    <>
      <div className="my-1 min-w-0" data-codex-subagent-activity="">
        <TimelineTimestamp className="mb-0.5 block" timestamp={timestamp} />
        {canReadTrace ? (
          <button
            aria-label={copy.openTrace}
            className="flex w-full min-w-0 items-center gap-1.5 rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2 text-left transition-colors hover:bg-(--ui-surface-hover) focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={openRootTrace}
            type="button"
          >
            <span className={SCAFFOLD_GLYPH_CLASS}>
              <Codicon className="text-(--ui-text-tertiary)" name="agent" size="0.8rem" />
            </span>
            <span className={cn(SCAFFOLD_LABEL_CLASS, 'min-w-0 truncate')}>{label}</span>
            <span className={SCAFFOLD_META_CLASS}>{status}</span>
            {meta.length > 0 && <span className={cn(SCAFFOLD_META_CLASS, 'hidden sm:inline')}>{meta.join(' · ')}</span>}
            <span aria-hidden className="ml-auto shrink-0 text-(--ui-text-tertiary)">
              ↗
            </span>
          </button>
        ) : (
          <div
            className="flex min-w-0 items-center gap-1.5 rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2"
            role="status"
          >
            <span className={SCAFFOLD_GLYPH_CLASS}>
              <Codicon className="text-(--ui-text-tertiary)" name="agent" size="0.8rem" />
            </span>
            <span className={cn(SCAFFOLD_LABEL_CLASS, 'min-w-0 truncate')}>{copy.traceUnavailable}</span>
            <span className={SCAFFOLD_META_CLASS}>{status}</span>
          </div>
        )}
      </div>

      <CodexSubagentTraceDialog
        description={[status, ...meta].filter(Boolean).join(' · ')}
        messages={messages}
        onBack={returnToParentTrace}
        onOpenChange={setOpen}
        onOpenNested={openNestedTrace}
        onRetry={() => setRefreshKey(key => key + 1)}
        open={open}
        trace={trace}
        traceDepth={traceStack.length}
      />
    </>
  )
}

const CodiconAgent: FC<{ className?: string }> = ({ className }) => (
  <Codicon className={className} name="agent" size="0.8rem" />
)
