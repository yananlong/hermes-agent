import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $currentBranch,
  $currentCwd,
  $newChatWorkspaceTarget,
  setCurrentBranch,
  setCurrentCwd,
  setCurrentCwdTransient,
  setNewChatWorkspaceTarget
} from '@/store/session'

import { deferred } from '../../../test/deferred'

import { useCwdActions } from './use-cwd-actions'

const sessionApiMocks = vi.hoisted(() => ({ moveStoredLinkedSessionWorkspace: vi.fn() }))
const notificationMocks = vi.hoisted(() => ({ notifyError: vi.fn() }))

vi.mock('@/api/sessions', async importOriginal => ({
  ...(await importOriginal<object>()),
  moveStoredLinkedSessionWorkspace: sessionApiMocks.moveStoredLinkedSessionWorkspace
}))

vi.mock('@/store/notifications', async importOriginal => ({
  ...(await importOriginal<object>()),
  notifyError: notificationMocks.notifyError
}))

type CwdActionsHandle = ReturnType<typeof useCwdActions>

function Harness({
  activeSessionIdRef,
  onReady,
  requestGateway,
  onSessionRuntimeInfo,
  refreshSessions
}: {
  activeSessionIdRef: MutableRefObject<string | null>
  onReady: (handle: CwdActionsHandle) => void
  onSessionRuntimeInfo?: (sessionId: string, info: { branch?: string; cwd?: string }, storedSessionId?: string) => void
  refreshSessions?: () => Promise<void>
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const actions = useCwdActions({
    activeSessionIdRef,
    onSessionRuntimeInfo,
    refreshSessions,
    requestGateway
  })

  useEffect(() => {
    onReady(actions)
  }, [actions, onReady])

  return null
}

describe('useCwdActions session target', () => {
  afterEach(() => {
    cleanup()
    setCurrentCwd('')
    setCurrentBranch('')
    sessionApiMocks.moveStoredLinkedSessionWorkspace.mockReset()
    notificationMocks.notifyError.mockReset()
  })

  it('re-homes the named tile, not the primary, and leaves the primary readout alone', async () => {
    const requestGateway = vi.fn(async () => ({ branch: 'dev', cwd: '/opt/data/profiles/austin' }) as never)
    const onSessionRuntimeInfo = vi.fn()
    const activeSessionIdRef: MutableRefObject<string | null> = { current: 'primary-rt' }
    let handle: CwdActionsHandle | null = null
    setCurrentCwd('/primary-workspace')

    function TargetHarness() {
      const actions = useCwdActions({ activeSessionIdRef, onSessionRuntimeInfo, requestGateway })

      useEffect(() => {
        handle = actions
      }, [actions])

      return null
    }

    render(<TargetHarness />)
    await waitFor(() => expect(handle).not.toBeNull())

    await act(async () => {
      await handle!.changeSessionCwd('/opt/data/profiles/austin', 'tile-rt')
    })

    expect(requestGateway).toHaveBeenCalledWith('session.cwd.set', {
      cwd: '/opt/data/profiles/austin',
      session_id: 'tile-rt'
    })
    expect(onSessionRuntimeInfo).toHaveBeenCalledWith('tile-rt', { branch: 'dev', cwd: '/opt/data/profiles/austin' })
    expect($currentCwd.get()).toBe('/primary-workspace')

    await act(async () => {
      await handle!.changeSessionCwd('/opt/data/profiles/austin')
    })

    expect(requestGateway).toHaveBeenLastCalledWith('session.cwd.set', {
      cwd: '/opt/data/profiles/austin',
      session_id: 'primary-rt'
    })
    expect($currentCwd.get()).toBe('/opt/data/profiles/austin')
  })

  it('moves a cold linked row by stored id and never stages a new-chat cwd', async () => {
    sessionApiMocks.moveStoredLinkedSessionWorkspace.mockResolvedValue({ cwd: '/home' })
    const requestGateway = vi.fn(async () => ({}) as never)
    const refreshSessions = vi.fn(async () => {})
    const activeSessionIdRef: MutableRefObject<string | null> = { current: null }
    const owner = { connectionId: 'local', profile: 'default' }
    let handle: CwdActionsHandle | null = null

    function TargetHarness() {
      const actions = useCwdActions({ activeSessionIdRef, refreshSessions, requestGateway })

      useEffect(() => {
        handle = actions
      }, [actions])

      return null
    }

    render(<TargetHarness />)
    await waitFor(() => expect(handle).not.toBeNull())

    await act(async () => {
      await handle!.changeSessionCwd('/home', 'stored-codex', { kind: 'stored-linked-codex', owner })
    })

    expect(sessionApiMocks.moveStoredLinkedSessionWorkspace).toHaveBeenCalledWith(
      'stored-codex', '/home', requestGateway, owner
    )
    expect(requestGateway).not.toHaveBeenCalled()
    expect(refreshSessions).toHaveBeenCalledOnce()
    expect($newChatWorkspaceTarget.get()).toBeUndefined()
  })

  it('updates a resumed linked runtime from the canonical result without targeting another session', async () => {
    sessionApiMocks.moveStoredLinkedSessionWorkspace.mockResolvedValue({ cwd: '/home', branch: 'main' })
    const requestGateway = vi.fn(async () => ({}) as never)
    const onSessionRuntimeInfo = vi.fn()
    const refreshSessions = vi.fn(async () => {})
    const activeSessionIdRef: MutableRefObject<string | null> = { current: 'runtime-codex' }
    let handle: CwdActionsHandle | null = null

    render(
      <Harness
        activeSessionIdRef={activeSessionIdRef}
        onReady={h => (handle = h)}
        onSessionRuntimeInfo={onSessionRuntimeInfo}
        refreshSessions={refreshSessions}
        requestGateway={requestGateway}
      />
    )
    await waitFor(() => expect(handle).not.toBeNull())

    await act(async () => {
      await handle!.changeSessionCwd('/home', 'stored-codex', {
        kind: 'stored-linked-codex',
        runtimeId: 'runtime-codex'
      })
    })

    expect(onSessionRuntimeInfo).toHaveBeenCalledWith(
      'runtime-codex', { branch: 'main', cwd: '/home' }, 'stored-codex'
    )
    expect($currentCwd.get()).toBe('/home')
    expect($currentBranch.get()).toBe('main')
    expect(refreshSessions).toHaveBeenCalledOnce()
  })

  it('uses Codex-specific busy copy only when the owner identifies a native writer lock', async () => {
    const requestGateway = vi.fn(async () => ({}) as never)
    const refreshSessions = vi.fn(async () => {})
    const activeSessionIdRef: MutableRefObject<string | null> = { current: null }
    let handle: CwdActionsHandle | null = null

    render(
      <Harness activeSessionIdRef={activeSessionIdRef} onReady={h => (handle = h)} requestGateway={requestGateway} />
    )
    await waitFor(() => expect(handle).not.toBeNull())

    sessionApiMocks.moveStoredLinkedSessionWorkspace.mockRejectedValueOnce(Object.assign(new Error('session_busy'), {
      data: {
        category: 'busy',
        detail: 'Close this thread in the other Codex client and retry',
        reason: 'session_busy'
      }
    }))
    await act(async () => {
      await handle!.changeSessionCwd('/home', 'stored-codex', { kind: 'stored-linked-codex' })
    })

    expect(notificationMocks.notifyError).toHaveBeenLastCalledWith(
      expect.objectContaining({ message: 'Close this thread in Codex, then retry the workspace change.' }),
      'Working directory change failed'
    )

    sessionApiMocks.moveStoredLinkedSessionWorkspace.mockRejectedValueOnce(Object.assign(new Error('session_busy'), {
      data: { category: 'busy', reason: 'session_busy' }
    }))
    await act(async () => {
      await handle!.changeSessionCwd('/home', 'stored-codex', { kind: 'stored-linked-codex' })
    })

    expect(notificationMocks.notifyError).toHaveBeenLastCalledWith(
      expect.objectContaining({ message: 'Stop the running turn, then move the chat' }),
      'Working directory change failed'
    )
    expect(refreshSessions).not.toHaveBeenCalled()
  })
})

describe('useCwdActions draft workspace target', () => {
  beforeEach(() => {
    setCurrentCwd('')
    setCurrentBranch('')
    setNewChatWorkspaceTarget(undefined)
  })

  afterEach(() => {
    cleanup()
    setCurrentCwd('')
    setCurrentBranch('')
    setNewChatWorkspaceTarget(undefined)
    vi.restoreAllMocks()
  })

  it('ignores stale draft cwd normalization after a newer no-workspace target wins', async () => {
    const projectInfo = deferred<{ branch?: string; cwd?: string }>()
    const requestGateway = vi.fn(async () => projectInfo.promise as never)
    const activeSessionIdRef: MutableRefObject<string | null> = { current: null }
    let handle: CwdActionsHandle | null = null

    render(
      <Harness activeSessionIdRef={activeSessionIdRef} onReady={h => (handle = h)} requestGateway={requestGateway} />
    )
    await waitFor(() => expect(handle).not.toBeNull())

    let pendingChange!: Promise<void>

    await act(async () => {
      pendingChange = handle!.changeSessionCwd('/stale-workspace')
    })

    expect($newChatWorkspaceTarget.get()).toBe('/stale-workspace')

    setNewChatWorkspaceTarget(null)
    setCurrentCwdTransient('')
    projectInfo.resolve({ branch: 'main', cwd: '/normalized-stale-workspace' })

    await act(async () => {
      await pendingChange
    })

    expect($newChatWorkspaceTarget.get()).toBeNull()
    expect($currentCwd.get()).toBe('')
    expect($currentBranch.get()).toBe('')
  })
})
