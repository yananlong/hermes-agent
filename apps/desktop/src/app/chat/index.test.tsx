import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { assistantTextPart, type ChatMessage } from '@/lib/chat-messages'
import {
  $activeSessionId,
  $awaitingResponse,
  $busy,
  $contextSuggestions,
  $currentCwd,
  $currentModel,
  $currentProvider,
  $freshDraftReady,
  $gatewayState,
  $messages,
  $selectedStoredSessionId,
  $sessions
} from '@/store/session'

const threadRenderCount = vi.hoisted(() => ({ current: 0 }))
// Linked host capability: undefined = the gateway predates `session.foreign.status`.
const hostCapabilityState = vi.hoisted(() => ({ current: undefined as unknown }))

vi.mock('../session-import/use-host-capability', () => ({
  useHostCapability: () => hostCapabilityState.current
}))

vi.mock('@/components/assistant-ui/thread', async () => {
  const React = await import('react')

  return {
    Thread: () => {
      threadRenderCount.current += 1

      return React.createElement('div', { 'data-testid': 'thread' })
    }
  }
})

vi.mock('@/components/Backdrop', async () => {
  const React = await import('react')

  return { Backdrop: () => React.createElement('div', { 'data-testid': 'backdrop' }) }
})

vi.mock('@/components/prompt-overlays', () => ({ PromptOverlays: () => null }))
vi.mock('@/components/chat/vibe-hearts', () => ({ COMPOSER_HEART_CONFIG: {}, HeartField: () => null }))
vi.mock('@/lib/model-options', () => ({
  currentModelCapabilities: () => undefined,
  modelOptionsQueryKey: (...parts: unknown[]) => ['model-options', ...parts],
  requestModelOptions: vi.fn(async () => ({ models: [] }))
}))
vi.mock('./chat-drop-overlay', () => ({ ChatDropOverlay: () => null }))
vi.mock('./chat-swap-overlay', () => ({ ChatSwapOverlay: () => null, ChatSyncBadge: () => null }))
vi.mock('./composer', () => ({ ChatBar: () => null, ChatBarFallback: () => null }))
vi.mock('./hooks/use-file-drop-zone', () => ({
  useFileDropZone: () => ({ dragKind: null, dropHandlers: {} })
}))
vi.mock('./sidebar/session-actions-menu', async () => {
  const React = await import('react')

  return {
    SessionActionsMenu: ({ children }: { children: React.ReactNode }) =>
      React.createElement('div', { 'data-testid': 'session-actions-menu' }, children)
  }
})

const { ChatView } = await import('./index')

function assistantMessage(id: string, text: string): ChatMessage {
  return {
    id,
    parts: [assistantTextPart(text)],
    role: 'assistant'
  }
}

describe('ChatView render isolation', () => {
  beforeEach(() => {
    threadRenderCount.current = 0
    $activeSessionId.set('runtime-1')
    $awaitingResponse.set(false)
    $busy.set(false)
    $contextSuggestions.set([])
    $currentCwd.set('/work')
    $currentModel.set('test-model')
    $currentProvider.set('test-provider')
    $freshDraftReady.set(false)
    $gatewayState.set('closed')
    $messages.set([assistantMessage('assistant-1', 'Stable historical answer')])
    $selectedStoredSessionId.set('stored-1')
    $sessions.set([{ id: 'stored-1', message_count: 1, title: 'Stable chat' } as never])
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $activeSessionId.set(null)
    $awaitingResponse.set(false)
    $busy.set(false)
    $contextSuggestions.set([])
    $currentCwd.set('')
    $currentModel.set('')
    $currentProvider.set('')
    $freshDraftReady.set(false)
    $gatewayState.set('idle')
    $messages.set([])
    $selectedStoredSessionId.set(null)
    $sessions.set([])
  })

  it('does not re-render chat history when an unrelated parent idle tick updates', () => {
    const props = {
      gateway: null,
      maxVoiceRecordingSeconds: 120,
      onAddContextRef: vi.fn(),
      onAddUrl: vi.fn(),
      onAttachDroppedItems: vi.fn(),
      onAttachImageBlob: vi.fn(),
      onBranchInNewChat: vi.fn(),
      onCancel: vi.fn(),
      onDeleteSelectedSession: vi.fn(),
      onEdit: vi.fn(),
      onPasteClipboardImage: vi.fn(),
      onPickFiles: vi.fn(),
      onPickFolders: vi.fn(),
      onPickImages: vi.fn(),
      onReload: vi.fn(),
      onRemoveAttachment: vi.fn(),
      onRetryResume: vi.fn(),
      onSteer: vi.fn(),
      onSubmit: vi.fn(),
      onThreadMessagesChange: vi.fn(),
      onToggleSelectedPin: vi.fn(),
      onTranscribeAudio: vi.fn()
    }

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    function ParentTickHarness() {
      const [tick, setTick] = useState(0)

      return (
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/stored-1']}>
            <button onClick={() => setTick(value => value + 1)} type="button">
              parent tick {tick}
            </button>
            <ChatView {...props} />
          </MemoryRouter>
        </QueryClientProvider>
      )
    }

    render(<ParentTickHarness />)

    expect(screen.getByTestId('thread')).toBeTruthy()
    expect(threadRenderCount.current).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: /parent tick/i }))

    // memo(ChatView) with stable props must absorb the parent's idle tick —
    // the transcript (Thread) must not re-render. This is PR #38470's contract.
    expect(threadRenderCount.current).toBe(1)
  })

  describe('linked foreign sessions render read-only', () => {
    const linkedProps = {
      gateway: null,
      maxVoiceRecordingSeconds: 120,
      onAddContextRef: vi.fn(),
      onAddUrl: vi.fn(),
      onAttachDroppedItems: vi.fn(),
      onAttachImageBlob: vi.fn(),
      onBranchInNewChat: vi.fn(),
      onCancel: vi.fn(),
      onDeleteSelectedSession: vi.fn(),
      onEdit: vi.fn(),
      onPasteClipboardImage: vi.fn(),
      onPickFiles: vi.fn(),
      onPickFolders: vi.fn(),
      onPickImages: vi.fn(),
      onReload: vi.fn(),
      onRemoveAttachment: vi.fn(),
      onRetryResume: vi.fn(),
      onSteer: vi.fn(),
      onSubmit: vi.fn(),
      onThreadMessagesChange: vi.fn(),
      onToggleSelectedPin: vi.fn(),
      onTranscribeAudio: vi.fn()
    }

    function renderChat() {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } }
      })

      return render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/stored-1']}>
            <ChatView {...linkedProps} />
          </MemoryRouter>
        </QueryClientProvider>
      )
    }

    afterEach(() => {
      hostCapabilityState.current = undefined
    })

    it.each([
      [{ claude_linked: true }],
      [{ mistral_linked: true }],
      [{ codex_linked: true }]
    ])('shows the linked read-only note and no composer for %o', flags => {
      $sessions.set([{ id: 'stored-1', message_count: 1, title: 'Linked chat', ...flags } as never])

      renderChat()

      expect(screen.getByTestId('thread')).toBeTruthy()
      expect(screen.getByRole('note').textContent).toContain('native tool')
    })

    it('says a writable linked send lands in the native thread on the host', () => {
      hostCapabilityState.current = {
        state: 'ready',
        status: {
          host: 'studio',
          agents: [
            {
              agent: 'claude',
              label: 'Claude',
              store: { available: true, readable: true, reason_code: null, reason: null },
              cli: {
                available: true,
                command: 'claude',
                path: '~/.local/bin/claude',
                version: null,
                source: 'path',
                reason_code: null,
                reason: null
              },
              continue_status: { supported: true, writable: true, reason_code: null, reason: null }
            }
          ]
        }
      }
      $sessions.set([{ id: 'stored-1', message_count: 1, title: 'Linked chat', claude_linked: true } as never])

      renderChat()

      const note = screen.getByRole('note')
      expect(note.getAttribute('data-linked-appendable')).toBe('true')
      expect(note.textContent).toContain(
        'New messages are appended to this Claude Code thread on the host and are visible to Claude Code there.'
      )
    })

    it('keeps the read-only note when the host can show but not append', () => {
      hostCapabilityState.current = {
        state: 'ready',
        status: {
          host: 'studio',
          agents: [
            {
              agent: 'claude',
              label: 'Claude',
              store: { available: true, readable: true, reason_code: null, reason: null },
              cli: {
                available: true,
                command: 'claude',
                path: null,
                version: null,
                source: 'path',
                reason_code: null,
                reason: null
              },
              continue_status: {
                supported: true,
                writable: false,
                reason_code: 'send_read_only',
                reason: 'read-only'
              }
            }
          ]
        }
      }
      $sessions.set([{ id: 'stored-1', message_count: 1, title: 'Linked chat', claude_linked: true } as never])

      renderChat()

      const note = screen.getByRole('note')
      expect(note.getAttribute('data-linked-appendable')).toBe('false')
      expect(note.getAttribute('data-linked-reason')).toBe('send_read_only')
      expect(note.textContent).toContain('Sending is read-only in this build')
    })

    it('shows neither the note nor a linked affordance for an ordinary session', () => {
      $sessions.set([{ id: 'stored-1', message_count: 1, title: 'Stable chat' } as never])

      renderChat()

      expect(screen.getByTestId('thread')).toBeTruthy()
      expect(screen.queryByRole('note')).toBeNull()
    })
  })
})
