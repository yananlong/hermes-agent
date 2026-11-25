import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { setSessionOwnerHint } from '@/store/session'

import { SESSION_IMPORT_CODEX_ROUTE } from '../routes'

import { codexThreadRequest, type ForeignImportResult, foreignRequest } from './api'
import type { HostCapability } from './capability'

import { SessionImportView } from './index'

const hostCapability = vi.hoisted(() => ({ current: undefined as HostCapability | undefined }))

vi.mock('./api', () => ({ codexThreadRequest: vi.fn(), foreignRequest: vi.fn() }))
vi.mock('./use-host-capability', () => ({ useHostCapability: () => hostCapability.current }))
vi.mock('@/store/session', async () => {
  // use-host-capability reads the connection state to re-probe after a reconnect.
  const { atom } = await import('nanostores')

  return { $gatewayState: atom('open'), setSessionOwnerHint: vi.fn() }
})
vi.mock('@/components/assistant-ui/markdown-text', () => ({
  MarkdownTextContent: ({ text }: { text: string }) => <p>{text}</p>
}))
vi.mock('../overlays/overlay-view', () => ({
  OverlayView: ({ children }: { children: React.ReactNode }) => <div>{children}</div>
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  hostCapability.current = undefined
})

const owner = { connectionId: 'workstation', profile: 'research' }

const session = {
  id: 'foreign-one',
  source: 'claude',
  label: 'Claude Code',
  title: 'Repair imports',
  cwd: '/work/project',
  mtime: 1000,
  turn_count: 2,
  excerpt: 'Help with imports'
}

function mount(onOpenSession = vi.fn(), initialEntry = '/') {
  return {
    onOpenSession,
    ...render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <SessionImportView onClose={vi.fn()} onOpenSession={onOpenSession} owner={owner} />
        </MemoryRouter>
      </QueryClientProvider>
    )
  }
}

it('browses without importing, then retries a failed import on the captured owner before opening', async () => {
  let attempts = 0
  vi.mocked(foreignRequest).mockImplementation(async (_owner, method) => {
    if (method === 'list') {
      return { sessions: [session], next_offset: null, host: 'studio', unreadable: 0 }
    }

    if (method === 'preview') {
      return { messages: [{ role: 'user', content: 'Please repair this' }], total: 1, already_imported: null }
    }

    if (++attempts === 1) {
      throw new Error('Connection interrupted')
    }

    return { session_id: 'durable-one', already_imported: false }
  })
  const { onOpenSession } = mount()
  fireEvent.click(await screen.findByRole('button', { name: /Repair imports/ }))
  await screen.findByText('Please repair this')
  expect(attempts).toBe(0)
  fireEvent.click(screen.getByRole('button', { name: 'Continue in Hermes' }))
  await screen.findByRole('alert')
  expect(onOpenSession).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue in Hermes' }))
  await waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('durable-one'))
  expect(setSessionOwnerHint).toHaveBeenCalledWith('durable-one', owner)
  expect(foreignRequest).toHaveBeenCalledWith(owner, 'import', { id: 'foreign-one' }, expect.any(AbortSignal))
})

it('does not navigate when an import finishes after the view has closed', async () => {
  let finish!: (result: ForeignImportResult) => void
  vi.mocked(foreignRequest).mockImplementation(async (_owner, method) => {
    if (method === 'list') {
      return { sessions: [session], next_offset: null, host: 'studio', unreadable: 0 }
    }

    if (method === 'preview') {
      return { messages: [], total: 0, already_imported: 'existing' }
    }

    return new Promise<ForeignImportResult>(resolve => {
      finish = resolve
    })
  })
  const { onOpenSession, unmount } = mount()
  fireEvent.click(await screen.findByRole('button', { name: /Repair imports/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Open in Hermes' }))
  await waitFor(() => expect(finish).toBeDefined())
  unmount()
  finish({ session_id: 'existing', already_imported: true })
  await waitFor(() => expect(setSessionOwnerHint).toHaveBeenCalledWith('existing', owner))
  expect(onOpenSession).not.toHaveBeenCalled()
})

it('offers linked Codex threads separately from snapshot imports', async () => {
  vi.mocked(foreignRequest).mockResolvedValue({ sessions: [], next_offset: null, host: 'studio', unreadable: 0 })
  vi.mocked(codexThreadRequest).mockResolvedValue({ threads: [], next_cursor: null })
  mount()
  fireEvent.click(screen.getByRole('button', { name: 'Open a Codex thread' }))
  await screen.findByText('No Codex threads found')
  expect(codexThreadRequest).toHaveBeenCalledWith(owner, 'list', { cursor: null, limit: 25 }, expect.any(AbortSignal))
})

it('opens the Codex chooser directly from its route and keeps the current connection/profile owner', async () => {
  vi.mocked(codexThreadRequest).mockResolvedValue({ threads: [], next_cursor: null })
  mount(vi.fn(), SESSION_IMPORT_CODEX_ROUTE)

  await screen.findByText('No Codex threads found')
  expect(screen.getByRole('heading', { name: 'Open a Codex thread' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Open a Codex thread' })).toBeNull()
  expect(foreignRequest).not.toHaveBeenCalled()
  expect(codexThreadRequest).toHaveBeenCalledWith(owner, 'list', { cursor: null, limit: 25 }, expect.any(AbortSignal))
})

it('disables a host picker whose native store is missing and names the host', async () => {
  hostCapability.current = {
    state: 'ready',
    status: {
      host: 'studio',
      agents: [
        {
          agent: 'claude',
          label: 'Claude',
          store: { available: false, readable: false, reason_code: 'store_missing', reason: 'none' },
          cli: {
            available: false,
            command: 'claude',
            path: null,
            version: null,
            source: 'path',
            reason_code: 'cli_missing',
            reason: 'none'
          },
          continue_status: {
            supported: true,
            writable: false,
            reason_code: 'store_missing',
            reason: 'none'
          }
        }
      ]
    }
  }
  vi.mocked(foreignRequest).mockResolvedValue({ sessions: [], next_offset: null, host: 'studio', unreadable: 0 })
  mount()

  const claudeButton = await screen.findByRole('button', { name: 'Open a Claude Code session' })
  expect(claudeButton).toHaveProperty('disabled', true)
  expect(screen.getByText('No readable Claude Code session store on studio.')).toBeTruthy()
})

it('keeps every picker enabled against a gateway with no capability method', async () => {
  // No capability answer at all: an older gateway answers -32601, and the picker is unchanged.
  vi.mocked(foreignRequest).mockResolvedValue({ sessions: [session], next_offset: null, host: 'studio', unreadable: 0 })
  mount()

  const claudeButton = await screen.findByRole('button', { name: 'Open a Claude Code session' })
  expect(claudeButton).toHaveProperty('disabled', false)
})
