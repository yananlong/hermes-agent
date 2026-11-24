import { type MutableRefObject, useCallback } from 'react'

import type { ProfileScope } from '@/api/client'
import { moveStoredLinkedSessionWorkspace } from '@/api/sessions'
import { translateNow, useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'
import {
  $currentCwd,
  $newChatWorkspaceTargetGeneration,
  setCurrentBranch,
  setCurrentCwd,
  setCurrentCwdExplicit,
  setNewChatWorkspaceTarget
} from '@/store/session'
import type { SessionRuntimeInfo } from '@/types/hermes'

interface CwdActionsOptions {
  activeSessionIdRef: MutableRefObject<string | null>
  onSessionRuntimeInfo?: (
    sessionId: string,
    info: Pick<SessionRuntimeInfo, 'branch' | 'cwd'>,
    storedSessionId?: string
  ) => void
  refreshSessions?: () => Promise<void>
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

export interface CwdChangeTarget {
  kind: 'stored-linked-codex'
  owner?: ProfileScope
  /** Renderer runtime id, if the stored linked row is currently resumed. */
  runtimeId?: string | null
}

// The shared owner refuses a re-home while a turn runs (its frozen policy would change under it).
function busyReason(err: unknown): string | undefined {
  return (err as { data?: { reason?: string } } | null)?.data?.reason
}

function isCodexWriterBusy(err: unknown): boolean {
  const detail = (err as { data?: { detail?: unknown }; message?: unknown } | null)?.data?.detail
  const message = (err as { message?: unknown } | null)?.message
  const text = typeof detail === 'string' ? detail : typeof message === 'string' ? message : ''

  return /\bCodex\b|active writer/i.test(text)
}

function cwdChangeError(err: unknown, linkedCodex = false): unknown {
  const busy = busyReason(err) === 'session_busy'

  if (!busy) {
    return err
  }

  return new Error(linkedCodex && isCodexWriterBusy(err)
    ? translateNow('rightSidebar.linkedWorkspaceBusy')
    : translateNow('sidebar.projects.moveBusy'))
}

async function changeStoredLinkedSessionCwd({
  cwd,
  targetSessionId,
  target,
  activeSessionIdRef,
  onSessionRuntimeInfo,
  refreshSessions,
  requestGateway,
  cwdChangeFailed
}: {
  cwd: string
  targetSessionId: string | undefined
  target: CwdChangeTarget
  activeSessionIdRef: MutableRefObject<string | null>
  onSessionRuntimeInfo: CwdActionsOptions['onSessionRuntimeInfo']
  refreshSessions: CwdActionsOptions['refreshSessions']
  requestGateway: CwdActionsOptions['requestGateway']
  cwdChangeFailed: string
}): Promise<void> {
  // A cold stored row has no runtime id. Keep the stored id explicit and use
  // a fresh HTTP mutation snapshot; never fall through to the active session
  // or the new-chat workspace draft.
  if (!targetSessionId) {
    throw new Error('The linked session is unavailable; reopen it and try again.')
  }

  try {
    const info = await moveStoredLinkedSessionWorkspace<{ branch?: string; cwd?: string }>(
      targetSessionId,
      cwd,
      requestGateway,
      target.owner
    )

    const effectiveCwd = info.cwd || cwd
    const branch = info.branch || ''

    if (target.runtimeId) {
      onSessionRuntimeInfo?.(target.runtimeId, { branch, cwd: effectiveCwd }, targetSessionId)
    }

    if (target.runtimeId && target.runtimeId === activeSessionIdRef.current) {
      setCurrentCwd(effectiveCwd)
      setCurrentBranch(branch)
    }
  } catch (err) {
    notifyError(cwdChangeError(err, true), cwdChangeFailed)

    return
  }

  // A refresh is presentation only; it cannot undo an acknowledged move.
  try { await refreshSessions?.() } catch { /* the next normal list refresh will reconcile it */ }
}

async function changeOrdinarySessionCwd({
  cwd,
  sessionId,
  activeSessionIdRef,
  onSessionRuntimeInfo,
  requestGateway,
  copy
}: {
  cwd: string
  sessionId: string
  activeSessionIdRef: MutableRefObject<string | null>
  onSessionRuntimeInfo: CwdActionsOptions['onSessionRuntimeInfo']
  requestGateway: CwdActionsOptions['requestGateway']
  copy: Pick<ReturnType<typeof useI18n>['t']['desktop'], 'cwdChangeFailed' | 'cwdStagedTitle' | 'cwdStagedMessage'>
}): Promise<void> {
  const primary = sessionId === activeSessionIdRef.current

  try {
    const info = await requestGateway<SessionRuntimeInfo>('session.cwd.set', {
      session_id: sessionId,
      cwd
    })

    if (primary) {
      setCurrentCwd(info.cwd || cwd)
      setCurrentBranch(info.branch || '')
    }

    onSessionRuntimeInfo?.(sessionId, { branch: info.branch || '', cwd: info.cwd || cwd })
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)

    if (!message.includes('unknown method')) {
      notifyError(cwdChangeError(err), copy.cwdChangeFailed)

      return
    }

    if (primary) {
      setCurrentCwd(cwd)
      setCurrentBranch('')
    }

    notify({
      kind: 'warning',
      title: copy.cwdStagedTitle,
      message: copy.cwdStagedMessage
    })
  }
}

export function useCwdActions({ activeSessionIdRef, onSessionRuntimeInfo, refreshSessions, requestGateway }: CwdActionsOptions) {
  const { t } = useI18n()
  const copy = t.desktop

  const refreshProjectBranch = useCallback(
    async (cwd: string) => {
      const target = cwd.trim()

      if (!target || activeSessionIdRef.current) {
        return
      }

      try {
        const info = await requestGateway<{ branch?: string; cwd?: string }>('config.get', {
          key: 'project',
          cwd: target
        })

        if (!activeSessionIdRef.current && ($currentCwd.get() || target) === (info.cwd || target)) {
          setCurrentBranch(info.branch || '')
        }
      } catch {
        setCurrentBranch('')
      }
    },
    [activeSessionIdRef, requestGateway]
  )

  const changeSessionCwd = useCallback(
    async (cwd: string, targetSessionId?: string, target?: CwdChangeTarget) => {
      const trimmed = cwd.trim()

      if (!trimmed) {
        return
      }

      // A focused tile names its own runtime id. Otherwise the ref, not the
      // closure-captured prop: this hook's consumers are memoized on a stable
      // actions object, so the prop can still name the previously focused
      // chat. Re-anchoring the wrong session's workspace would point that
      // agent's terminal/file tools at another conversation's project.
      const sessionId = targetSessionId || activeSessionIdRef.current

      if (target?.kind === 'stored-linked-codex') {
        await changeStoredLinkedSessionCwd({
          cwd: trimmed,
          targetSessionId,
          target,
          activeSessionIdRef,
          onSessionRuntimeInfo,
          refreshSessions,
          requestGateway,
          cwdChangeFailed: copy.cwdChangeFailed
        })

        return
      }

      if (!sessionId) {
        setCurrentCwd(trimmed)
        // A folder chosen here is a deliberate workspace pick (#52589) — the
        // gateway must let it beat a named profile's configured terminal.cwd.
        setCurrentCwdExplicit(true)
        const workspaceGeneration = setNewChatWorkspaceTarget(trimmed)

        try {
          const info = await requestGateway<{ branch?: string; cwd?: string }>('config.get', {
            key: 'project',
            cwd: trimmed
          })

          if ($newChatWorkspaceTargetGeneration.get() !== workspaceGeneration || activeSessionIdRef.current) {
            return
          }

          // Adopt the backend's normalized cwd so the persisted workspace and
          // branch stay consistent with what the agent will use.
          if (info.cwd) {
            setCurrentCwd(info.cwd)
            setNewChatWorkspaceTarget(info.cwd)
          }

          setCurrentBranch(info.branch || '')
        } catch {
          if ($newChatWorkspaceTargetGeneration.get() === workspaceGeneration && !activeSessionIdRef.current) {
            setCurrentBranch('')
          }
        }

        return
      }

      await changeOrdinarySessionCwd({
        cwd: trimmed,
        sessionId,
        activeSessionIdRef,
        onSessionRuntimeInfo,
        requestGateway,
        copy
      })
    },
    [activeSessionIdRef, copy, onSessionRuntimeInfo, refreshSessions, requestGateway]
  )

  return { changeSessionCwd, refreshProjectBranch }
}
