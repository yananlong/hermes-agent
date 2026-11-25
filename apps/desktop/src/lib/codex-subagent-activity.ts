export const CODEX_SUBAGENT_ACTIVITY_TOOL = 'codex_subagent_activity'

export type CodexSubagentActivityKind = 'completed' | 'interacted' | 'interrupted' | 'started'

export interface CodexSubagentActivity {
  agentThreadId?: string
  kind?: CodexSubagentActivityKind
  model?: string
  reasoningEffort?: string
}

export interface LegacyCodexActivitySegment {
  activity: CodexSubagentActivity
  id: string
  type: 'activity'
}

export interface LegacyCodexTextSegment {
  text: string
  type: 'text'
}

export type LegacyCodexSegment = LegacyCodexActivitySegment | LegacyCodexTextSegment

const ACTIVITY_KINDS = new Set<CodexSubagentActivityKind>(['started', 'interacted', 'interrupted', 'completed'])
const LEGACY_MARKER = /^\[codex subAgentActivity\][ \t]+(.+)$/gm

const recordOf = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null

function stringField(record: Record<string, unknown>, names: string[], maxLength = 256): string | undefined {
  for (const name of names) {
    const value = record[name]

    if (typeof value === 'string' && value.trim()) {
      return value.trim().slice(0, maxLength)
    }
  }

  return undefined
}

export function codexSubagentActivityFromArgs(value: unknown): CodexSubagentActivity {
  let parsed = value

  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed)
    } catch {
      return {}
    }
  }

  const record = recordOf(parsed)

  if (!record) {
    return {}
  }

  const rawKind = stringField(record, ['kind', 'status'])

  const kind =
    rawKind && ACTIVITY_KINDS.has(rawKind as CodexSubagentActivityKind)
      ? (rawKind as CodexSubagentActivityKind)
      : undefined

  const agentThreadId = stringField(record, ['agent_thread_id', 'agentThreadId', 'thread_id', 'threadId'])
  const model = stringField(record, ['model'], 128)
  const reasoningEffort = stringField(record, ['reasoning_effort', 'reasoningEffort'], 64)

  return {
    ...(kind ? { kind } : {}),
    ...(agentThreadId ? { agentThreadId } : {}),
    ...(model ? { model } : {}),
    ...(reasoningEffort ? { reasoningEffort } : {})
  }
}

function legacyActivitySegment(raw: string, index: number): LegacyCodexActivitySegment | null {
  let value: unknown

  try {
    value = JSON.parse(raw)
  } catch {
    return null
  }

  const record = recordOf(value)

  if (!record) {
    return null
  }

  const activity = codexSubagentActivityFromArgs(record)
  const id = stringField(record, ['id'], 128)

  return {
    type: 'activity',
    id: id ? `codex_subAgentActivity_${id}` : `codex_subAgentActivity_legacy_${index}`,
    activity
  }
}

/** Turn the old opaque Codex line into a display segment while leaving every other byte visible. */
export function splitLegacyCodexSubagentActivity(text: string): LegacyCodexSegment[] {
  const out: LegacyCodexSegment[] = []
  let cursor = 0
  let match: RegExpExecArray | null

  LEGACY_MARKER.lastIndex = 0

  while ((match = LEGACY_MARKER.exec(text))) {
    const activity = legacyActivitySegment(match[1], out.length)

    if (!activity) {
      continue
    }

    if (match.index > cursor) {
      out.push({ type: 'text', text: text.slice(cursor, match.index) })
    }

    out.push(activity)
    cursor = match.index + match[0].length
  }

  if (cursor === 0) {
    return [{ type: 'text', text }]
  }

  if (cursor < text.length) {
    out.push({ type: 'text', text: text.slice(cursor) })
  }

  return out
}
