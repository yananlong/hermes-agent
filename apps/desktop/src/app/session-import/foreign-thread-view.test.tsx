import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setSessionOwnerHint } from '@/store/session'

import { linkedThreadRequest } from './api'
import type { HostCapability } from './capability'
import { ForeignThreadView } from './foreign-thread-view'

const hostCapability = vi.hoisted(() => ({ current: undefined as HostCapability | undefined }))

vi.mock('./api', () => ({ linkedThreadRequest: vi.fn() }))
vi.mock('./use-host-capability', () => ({ useHostCapability: () => hostCapability.current }))
vi.mock('@/store/session', () => ({ setSessionOwnerHint: vi.fn() }))
vi.mock('../overlays/overlay-view', () => ({
  OverlayView: ({ children }: { children: React.ReactNode }) => <div>{children}</div>
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  hostCapability.current = undefined
})

function capability(overrides: { available?: boolean; writable?: boolean } = {}): HostCapability {
  const available = overrides.available ?? true

  return {
    state: 'ready',
    status: {
      host: 'studio',
      agents: [
        {
          agent: 'claude',
          label: 'Claude',
          store: available
            ? { available: true, readable: true, reason_code: null, reason: null }
            : { available: false, readable: false, reason_code: 'store_missing', reason: 'No Claude Code store.' },
          cli: available
            ? { available: true, command: 'claude', path: '~/.local/bin/claude', version: null, source: 'path', reason_code: null, reason: null }
            : { available: false, command: 'claude', path: null, version: null, source: 'path', reason_code: 'cli_missing', reason: 'Not installed.' },
          continue_status: overrides.writable
            ? { supported: true, writable: true, reason_code: null, reason: null }
            : { supported: true, writable: false, reason_code: 'send_read_only', reason: 'Read-only build.' }
        }
      ]
    }
  }
}

const owner = { connectionId: 'workstation', profile: 'research' }

const session = {
  id: '0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d',
  title: 'Claude refactor',
  cwd: '/home',
  updated_at: 1,
  preview: '',
  source: 'claude-code'
}

function mount(onOpenSession = vi.fn()) {
  return {
    onOpenSession,
    ...render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ForeignThreadView
          agent="claude"
          onBack={vi.fn()}
          onClose={vi.fn()}
          onOpenSession={onOpenSession}
          owner={owner}
        />
      </QueryClientProvider>
    )
  }
}

it('opens the selected native session as a pointer without importing its transcript', async () => {
  vi.mocked(linkedThreadRequest).mockImplementation(async (_owner, _agent, method) =>
    method === 'list'
      ? { threads: [session], next_cursor: null }
      : { session_id: 'linked-session', already_linked: false }
  )
  const { onOpenSession } = mount()
  fireEvent.click(await screen.findByRole('button', { name: /Claude refactor/ }))
  expect(screen.getByRole('note').textContent).toContain('reads its history live from the native tool')
  fireEvent.click(screen.getByRole('button', { name: 'Open linked session' }))
  await waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('linked-session'))
  expect(setSessionOwnerHint).toHaveBeenCalledWith('linked-session', owner)
  expect(linkedThreadRequest).toHaveBeenCalledWith(
    owner,
    'claude',
    'link',
    { session_id: session.id },
    expect.any(AbortSignal)
  )
})

it('shows an error and does not navigate if the backend cannot link the session', async () => {
  vi.mocked(linkedThreadRequest).mockImplementation(async (_owner, _agent, method) => {
    if (method === 'list') {
      return { threads: [session], next_cursor: null }
    }

    throw new Error('Session is busy')
  })
  const { onOpenSession } = mount()
  fireEvent.click(await screen.findByRole('button', { name: /Claude refactor/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Open linked session' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Session is busy')
  expect(onOpenSession).not.toHaveBeenCalled()
})
it('disables the link with the host reason when this gateway cannot read the native store', async () => {
  hostCapability.current = capability({ available: false })
  vi.mocked(linkedThreadRequest).mockResolvedValue({ threads: [session], next_cursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Claude refactor/ }))

  expect(screen.getByRole('button', { name: 'Open linked session' })).toHaveProperty('disabled', true)
  expect(screen.getByText('No readable Claude Code session store on studio.')).toBeTruthy()
  // The transcript itself is never gated on the capability answer: the row stays selectable.
  expect(screen.getByText(session.id)).toBeTruthy()
})

it('says a writable send lands in the native thread on the host', async () => {
  hostCapability.current = capability({ writable: true })
  vi.mocked(linkedThreadRequest).mockResolvedValue({ threads: [session], next_cursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Claude refactor/ }))

  expect(screen.getByRole('note').textContent).toContain(
    'New messages are appended to this Claude Code thread on the host and are visible to Claude Code there.'
  )
})

it('keeps the read-only fallback against a gateway that predates the capability check', async () => {
  // hostCapability stays undefined: an older gateway answers -32601 and the hook reports unknown.
  vi.mocked(linkedThreadRequest).mockResolvedValue({ threads: [session], next_cursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Claude refactor/ }))

  expect(screen.getByRole('button', { name: 'Open linked session' })).toHaveProperty('disabled', false)
  expect(screen.getByRole('note').textContent).toContain('reads its history live from the native tool')
})
