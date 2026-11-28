/** Convert already policy-filtered native display rows without losing tool or reasoning details. */
import type { Msg } from '../types.js'

import { toTranscriptMessages } from './messages.js'

const displayValue = (value: unknown): string =>
  typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? ''

export function toLinkedHistoryMessages(rows: Record<string, unknown>[]): Msg[] {
  const messages: Msg[] = []
  for (const row of rows) {
    if (row.display_kind === 'hidden') continue
    const reasoning = row.reasoning ?? row.reasoning_content
    if (row.role === 'tool') {
      const fields = [
        typeof row.name === 'string' ? row.name : 'tool',
        row.args ? `Arguments:\n${displayValue(row.args)}` : '',
        row.content ? `Result:\n${displayValue(row.content)}` : '',
        row.truncated ? '[This message was truncated by the gateway.]' : ''
      ].filter(Boolean)
      messages.push({ role: 'tool', text: fields.join('\n'), linkedHistory: true })
      continue
    }
    if (row.role === 'assistant' && row.tool_calls) {
      messages.push({ role: 'tool', text: `Tool calls:\n${displayValue(row.tool_calls)}`, linkedHistory: true })
    }
    const normal = toTranscriptMessages([row])
    if (reasoning && normal.length === 0) {
      messages.push({ role: 'system', kind: 'trail', text: '', thinking: displayValue(reasoning), linkedHistory: true })
    } else {
      messages.push(...normal.map(message => ({ ...message, linkedHistory: true,
        ...(reasoning ? { thinking: displayValue(reasoning) } : {}) })))
    }
  }
  return messages
}
