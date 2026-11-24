import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { I18nProvider } from '@/i18n/context'
import type { ChatMessage } from '@/lib/chat-messages'
import * as sessionStates from '@/store/session-states'

import { CodexSubagentActivity } from './codex-subagent-activity'

const PARENT = 'linked-parent-session'

function view(): SessionView {
  return {
    kind: 'primary',
    $runtimeId: atom<string | null>(PARENT),
    $storedId: atom<string | null>(PARENT),
    $messages: atom<ChatMessage[]>([]),
    $busy: atom(false),
    $awaitingResponse: atom(false),
    $messagesEmpty: atom(true),
    $lastVisibleIsUser: atom(false),
    $turnStartedAt: atom<null | number>(null),
    $cwd: atom(''),
    $model: atom(''),
    $provider: atom(''),
    $fast: atom(false),
    $reasoningEffort: atom(''),
    $reasoningEffortPending: atom(false),
    $reasoningEffortWire: atom('')
  } satisfies SessionView
}

function renderCard(
  args: unknown = { kind: 'completed', agent_thread_id: 'child-private-id', model: 'gpt-6.2-codex' }
) {
  const tree = (cardArgs: unknown) => (
    <I18nProvider configClient={null} initialLocale="en">
      <SessionViewProvider value={view()}>
        <CodexSubagentActivity args={cardArgs} timestamp={1_700_000_000} />
      </SessionViewProvider>
    </I18nProvider>
  )

  const rendered = render(tree(args))

  return { ...rendered, rerenderCard: (cardArgs: unknown) => rendered.rerender(tree(cardArgs)) }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('Codex subagent trace card', () => {
  it('shows Codex lifecycle kinds from activity data rather than the enclosing tool settlement', () => {
    const { rerenderCard } = renderCard({ kind: 'started', status: 'completed', agent_thread_id: 'child-live' })

    expect(screen.getByText('Started')).toBeTruthy()
    rerenderCard({ kind: 'completed', status: 'completed', agent_thread_id: 'child-live' })
    expect(screen.getByText('Completed')).toBeTruthy()
    expect(screen.queryByText('Started')).toBeNull()
  })

  it('opens the parent-authorized child transcript and renders messages, reasoning, tools, and results', async () => {
    const request = vi.spyOn(sessionStates, 'requestForOwnedSession').mockResolvedValue({
      available: true,
      running: false,
      reset: false,
      next_cursor: null,
      rows: [
        {
          role: 'assistant',
          text: 'I will inspect the changed file.',
          reasoning: 'Check the latest diff first.',
          timestamp: 12
        },
        {
          role: 'assistant',
          content: '',
          timestamp: 13,
          tool_calls: [
            {
              id: 'child-call-1',
              type: 'function',
              function: { name: 'terminal', arguments: JSON.stringify({ command: 'git diff --stat' }) }
            }
          ]
        },
        {
          role: 'tool',
          name: 'terminal',
          tool_name: 'terminal',
          tool_call_id: 'child-call-1',
          content: JSON.stringify({
            output: '1 file changed',
            agent_thread_id: 'hidden-child-id',
            agent_path: '/private/path'
          }),
          timestamp: 14
        }
      ]
    } as never)

    renderCard({
      kind: 'completed',
      agent_thread_id: 'child-private-id',
      agent_path: '/private/agent/config',
      model: 'gpt-6.2-codex'
    })

    expect(screen.getByRole('button', { name: 'Open subagent trace' })).toBeTruthy()
    expect(window.document.body.textContent).not.toContain('child-private-id')
    expect(window.document.body.textContent).not.toContain('/private/agent/config')
    fireEvent.click(screen.getByRole('button', { name: 'Open subagent trace' }))

    expect(await screen.findByRole('heading', { name: 'Codex subagent trace' })).toBeTruthy()
    expect(await screen.findByText('I will inspect the changed file.')).toBeTruthy()
    expect(screen.getByText('Reasoning summary')).toBeTruthy()
    expect(screen.getByText('Check the latest diff first.')).toBeTruthy()
    expect(screen.getByText('terminal')).toBeTruthy()
    expect(
      [...window.document.querySelectorAll('pre')].some(node => node.textContent?.includes('1 file changed'))
    ).toBe(true)
    expect(window.document.body.textContent).not.toContain('hidden-child-id')
    expect(window.document.body.textContent).not.toContain('/private/path')
    expect(request).toHaveBeenCalledWith(
      PARENT,
      expect.any(Function),
      'session.transcript',
      expect.objectContaining({
        session_id: PARENT,
        codex_subagent_thread_id: 'child-private-id',
        order: 'oldest',
        limit: 100
      }),
      30_000,
      expect.any(AbortSignal)
    )
    fireEvent.click(screen.getByRole('button', { name: 'Return to conversation' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('opens nested child traces and returns to the parent trace', async () => {
    const request = vi
      .spyOn(sessionStates, 'requestForOwnedSession')
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: null,
        rows: [
          {
            role: 'assistant',
            content: '',
            timestamp: 11,
            tool_calls: [
              {
                id: 'nested-activity-1',
                type: 'function',
                function: {
                  name: 'codex_subagent_activity',
                  arguments: JSON.stringify({ kind: 'started', agent_thread_id: 'nested-child-private-id' })
                }
              }
            ]
          },
          {
            role: 'assistant',
            content: '',
            timestamp: 11,
            tool_calls: [
              {
                id: 'nested-activity-2',
                type: 'function',
                function: {
                  name: 'codex_subagent_activity',
                  arguments: JSON.stringify({ kind: 'started', agent_thread_id: 'another-nested-child-id' })
                }
              }
            ]
          }
        ]
      } as never)
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: null,
        rows: [{ role: 'assistant', text: 'Nested child trace.' }]
      } as never)
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: null,
        rows: [{ role: 'assistant', text: 'Parent child trace.' }]
      } as never)

    renderCard({ kind: 'started', agent_thread_id: 'parent-child-private-id' })
    fireEvent.click(screen.getByRole('button', { name: 'Open subagent trace' }))

    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getAllByRole('button', { name: 'Open subagent trace' })).toHaveLength(2))
    expect(within(dialog).queryByText('Result')).toBeNull()
    fireEvent.click(within(dialog).getAllByRole('button', { name: 'Open subagent trace' })[0]!)

    expect(await within(dialog).findByText('Nested child trace.')).toBeTruthy()
    expect(request).toHaveBeenNthCalledWith(
      2,
      PARENT,
      expect.any(Function),
      'session.transcript',
      expect.objectContaining({ session_id: PARENT, codex_subagent_thread_id: 'nested-child-private-id' }),
      30_000,
      expect.any(AbortSignal)
    )
    expect(window.document.body.textContent).not.toContain('nested-child-private-id')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to parent trace' }))

    expect(await within(dialog).findByText('Parent child trace.')).toBeTruthy()
    expect(request).toHaveBeenNthCalledWith(
      3,
      PARENT,
      expect.any(Function),
      'session.transcript',
      expect.objectContaining({ session_id: PARENT, codex_subagent_thread_id: 'parent-child-private-id' }),
      30_000,
      expect.any(AbortSignal)
    )
  })

  it('honestly shows an unavailable trace when the activity has no readable target', () => {
    renderCard({ kind: 'completed', agent_path: '/private/path' })

    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByRole('status').textContent).toContain('Subagent trace unavailable')
    expect(window.document.body.textContent).not.toContain('/private/path')
  })

  it('reports unavailable history when the backend cannot authorize or read the child trace', async () => {
    vi.spyOn(sessionStates, 'requestForOwnedSession').mockResolvedValue({ available: false } as never)

    renderCard({ kind: 'interrupted', agent_thread_id: 'child-no-longer-readable' })
    fireEvent.click(screen.getByRole('button', { name: 'Open subagent trace' }))

    expect(await screen.findByText('Subagent trace unavailable')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it('drains short cursor pages and preserves distinct native tool calls with the same timestamp', async () => {
    vi.useFakeTimers()

    const toolCall = (id: string) => ({
      role: 'assistant',
      content: '',
      timestamp: 11,
      tool_calls: [
        {
          id,
          type: 'function',
          function: { name: 'terminal', arguments: '{}' }
        }
      ]
    })

    const request = vi
      .spyOn(sessionStates, 'requestForOwnedSession')
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: 'cursor-1',
        rows: [toolCall('native-call-1')]
      } as never)
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: 'cursor-2',
        rows: [toolCall('native-call-2')]
      } as never)
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: 'cursor-2',
        rows: []
      } as never)

    renderCard({ kind: 'started', agent_thread_id: 'child-paged' })
    fireEvent.click(screen.getByRole('button', { name: 'Open subagent trace' }))

    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      await vi.runAllTimersAsync()
    })

    expect(request).toHaveBeenCalledTimes(3)
    expect(request).toHaveBeenNthCalledWith(
      2,
      PARENT,
      expect.any(Function),
      'session.transcript',
      expect.objectContaining({ cursor: 'cursor-1', codex_subagent_thread_id: 'child-paged' }),
      30_000,
      expect.any(AbortSignal)
    )
    expect(screen.getAllByText('terminal')).toHaveLength(2)
  })

  it('shows a retryable error when history fails, then loads the trace on retry', async () => {
    const request = vi
      .spyOn(sessionStates, 'requestForOwnedSession')
      .mockRejectedValueOnce(new Error('native history unavailable'))
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: null,
        rows: [{ role: 'assistant', text: 'Recovered child history.' }]
      } as never)

    renderCard({ kind: 'interrupted', agent_thread_id: 'child-retry' })
    fireEvent.click(screen.getByRole('button', { name: 'Open subagent trace' }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('Recovered child history.')).toBeTruthy()
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('refreshes a live trace until the backend reports that the child has finished', async () => {
    vi.useFakeTimers()

    const request = vi
      .spyOn(sessionStates, 'requestForOwnedSession')
      .mockResolvedValueOnce({
        available: true,
        running: true,
        reset: false,
        next_cursor: 'cursor-1',
        rows: []
      } as never)
      .mockResolvedValueOnce({
        available: true,
        running: false,
        reset: false,
        next_cursor: 'cursor-2',
        rows: []
      } as never)

    renderCard({ kind: 'started', agent_thread_id: 'child-live' })
    fireEvent.click(screen.getByRole('button', { name: 'Open subagent trace' }))

    await act(async () => {
      await Promise.resolve()
    })
    expect(request).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(request).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenLastCalledWith(
      PARENT,
      expect.any(Function),
      'session.transcript',
      expect.objectContaining({ cursor: 'cursor-1', codex_subagent_thread_id: 'child-live' }),
      30_000,
      expect.any(AbortSignal)
    )
  })
})
