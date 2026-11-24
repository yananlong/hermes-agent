import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import { $toolDisclosureStates, setHideCodeDiffs, setToolDisclosureOpen, setToolViewMode } from '@/store/tool-view'
import type { SessionMessage } from '@/types/hermes'

import { toolEntryDisclosureId } from './fallback-model/targets'

vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-1', status: { type: 'complete' } }, thread: { isRunning: false } })
}))

const { ToolFallback } = await import('./fallback')

function nativeRows(toolName: string, callId: string, argumentsText: string, resultText: string): SessionMessage[] {
  return [
    { role: 'user', content: 'Continue the native thread.' },
    {
      role: 'assistant',
      content: null,
      display_metadata: { codex_native: true },
      tool_calls: [
        {
          id: callId,
          type: 'function',
          function: { name: toolName, arguments: argumentsText }
        }
      ]
    },
    {
      role: 'tool',
      content: resultText,
      display_metadata: { codex_native: true },
      tool_call_id: callId,
      tool_name: toolName
    }
  ] as SessionMessage[]
}

function renderHydratedTool(messages: SessionMessage[]) {
  const assistant = toChatMessages(messages).find(message => message.role === 'assistant')
  const part = assistant?.parts.find(item => item.type === 'tool-call')

  if (!part || part.type !== 'tool-call') {
    throw new Error('Expected the native tool pair to hydrate into an assistant tool call')
  }

  setToolDisclosureOpen(toolEntryDisclosureId('msg-1', part), true)

  return render(<ToolFallback {...(part as unknown as ComponentProps<typeof ToolFallback>)} />)
}

beforeEach(() => {
  setToolViewMode('product')
  setHideCodeDiffs(false)
})

afterEach(() => {
  cleanup()
  $toolDisclosureStates.set({})
  setToolViewMode('product')
  setHideCodeDiffs(false)
})

describe('Codex native tool payload disclosure', () => {
  it('shows both hydrated arguments and results in product mode while omitting encrypted fields', () => {
    const rendered = renderHydratedTool(
      nativeRows(
        'send_message',
        'codex_dynamicToolCall_item-1',
        JSON.stringify({ recipient: 'ARG_RECIPIENT_731', message: 'ARG_MESSAGE_731' }),
        JSON.stringify({ status: 'sent', message: 'RESULT_MESSAGE_731', encrypted_content: 'OPAQUE_SECRET_731' })
      )
    )

    fireEvent.click(screen.getByRole('button', { name: 'Tool payload' }))

    const transcript = rendered.container.textContent || ''

    expect(transcript).toContain('ARG_RECIPIENT_731')
    expect(transcript).toContain('ARG_MESSAGE_731')
    expect(transcript).toContain('RESULT_MESSAGE_731')
    expect(transcript).not.toContain('OPAQUE_SECRET_731')
  })

  it('shows native file-change diffs in product mode and honors hide-code-diffs', () => {
    const callId = 'codex_fileChange_item-2'
    const diff = '--- a/src/example.ts\n+++ b/src/example.ts\n+const result = 1'

    const messages = nativeRows(
      'apply_patch',
      callId,
      JSON.stringify({ changes: [{ kind: 'update', path: 'src/example.ts' }] }),
      `apply_patch status=completed, 1 change(s)\n\nupdate: src/example.ts\n${diff}`
    )

    const rendered = renderHydratedTool(messages)

    fireEvent.click(screen.getByRole('button', { name: 'Tool payload' }))
    expect(rendered.container.textContent).toContain('+const result = 1')

    cleanup()
    $toolDisclosureStates.set({})
    setHideCodeDiffs(true)
    const hidden = renderHydratedTool(messages)

    expect(screen.queryByRole('button', { name: 'Tool payload' })).toBeNull()
    expect(hidden.container.textContent).not.toContain('+const result = 1')
  })

  it('keeps the product-mode payload disclosure off ordinary Hermes calls', () => {
    renderHydratedTool(
      nativeRows(
        'send_message',
        'call_ordinary-1',
        JSON.stringify({ message: 'ordinary args' }),
        '{"message":"ordinary result"}'
      )
    )

    expect(screen.queryByRole('button', { name: 'Tool payload' })).toBeNull()
  })
})
