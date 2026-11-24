import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setSessionOwnerHint } from '@/store/session'

import { codexThreadRequest } from './api'
import { CodexThreadView } from './codex-thread-view'

vi.mock('./api', () => ({ codexThreadRequest: vi.fn() }))
vi.mock('@/store/session', () => ({ setSessionOwnerHint: vi.fn() }))
vi.mock('../overlays/overlay-view', () => ({
  OverlayView: ({ children }: { children: React.ReactNode }) => <div>{children}</div>
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const owner = { connectionId: 'workstation', profile: 'research' }

const thread = {
  id: 'thread-id',
  title: 'Codex investigation',
  cwd: '/home',
  updated_at: 1,
  preview: 'Prompt',
  source: 'cli'
}

function mount(onOpenSession = vi.fn()) {
  return {
    onOpenSession,
    ...render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CodexThreadView onBack={vi.fn()} onClose={vi.fn()} onOpenSession={onOpenSession} owner={owner} />
      </QueryClientProvider>
    )
  }
}

it('opens the selected native thread on the captured connection without importing its transcript', async () => {
  vi.mocked(codexThreadRequest).mockImplementation(async (_owner, method) =>
    method === 'list'
      ? { threads: [thread], next_cursor: null }
      : { session_id: 'linked-session', already_linked: false }
  )
  const { onOpenSession } = mount()
  fireEvent.click(await screen.findByRole('button', { name: /Codex investigation/ }))
  expect(screen.getByText('Close the thread in other clients before continuing here.')).toBeTruthy()
  expect(screen.getByRole('note').textContent).toContain('client-defined tools supplied by the original Codex client')
  expect(screen.getByRole('note').textContent).toContain('Hermes delegation callbacks')
  fireEvent.click(screen.getByRole('button', { name: 'Open linked thread' }))
  await waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('linked-session'))
  expect(setSessionOwnerHint).toHaveBeenCalledWith('linked-session', owner)
  expect(codexThreadRequest).toHaveBeenCalledWith(owner, 'link', { thread_id: 'thread-id' }, expect.any(AbortSignal))
})

it('shows an error and does not navigate if the backend cannot link the thread', async () => {
  vi.mocked(codexThreadRequest).mockImplementation(async (_owner, method) => {
    if (method === 'list') {
      return { threads: [thread], next_cursor: null }
    }

    throw new Error('Thread is busy')
  })
  const { onOpenSession } = mount()
  fireEvent.click(await screen.findByRole('button', { name: /Codex investigation/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Open linked thread' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Thread is busy')
  expect(onOpenSession).not.toHaveBeenCalled()
})
