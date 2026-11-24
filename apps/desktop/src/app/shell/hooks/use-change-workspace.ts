import { useCallback, useMemo } from 'react'

import type { ProfileScope } from '@/api/client'
import type { CwdChangeTarget } from '@/app/session/hooks/use-cwd-actions'
import { useI18n } from '@/i18n'
import { selectDesktopPaths } from '@/lib/desktop-fs'
import { displayPath } from '@/lib/display-path'
import { notifyError } from '@/store/notifications'

import type { StatusbarMenuItem } from '../statusbar-controls'

interface FocusedTarget {
  focusedStoredSessionId: null | string
  focusedStoredSessionLinkedCodex: boolean
  focusedWorkspaceOtherGateway: boolean
  focusedRuntimeId: null | string
  workspaceOwner?: ProfileScope
  primaryRuntimeId: null | string
  primaryFocused: boolean
}

/**
 * The workspace menu's "Change working directory" item: re-homes the FOCUSED
 * chat, the recovery path for a chat that started in the wrong folder, Bot
 * Chats included (they never reach the sidebar's move-to-project). The picker
 * is remote-aware, so a remote gateway browses its own filesystem. Stored
 * Linked Codex rows can also be moved while cold: a stored-row mutation snapshot
 * supplies the revision/generation fences without first resuming on this client.
 * A session owned by another gateway is hidden because the picker would browse
 * the wrong machine. Disabled while a turn runs: the backend refuses changes then.
 */
export function useChangeWorkspaceItem(
  changeSessionCwd: (cwd: string, sessionId?: string, target?: CwdChangeTarget) => Promise<void>,
  currentCwd: string,
  busy: boolean,
  {
    focusedStoredSessionId,
    focusedStoredSessionLinkedCodex,
    focusedWorkspaceOtherGateway,
    focusedRuntimeId,
    workspaceOwner,
    primaryRuntimeId,
    primaryFocused
  }: FocusedTarget
): StatusbarMenuItem {
  const copy = useI18n().t
  const title = copy.rightSidebar.changeCwdTitle
  const hasRuntime = primaryFocused ? Boolean(primaryRuntimeId) : Boolean(focusedRuntimeId)
  const linkedStoredSession = Boolean(focusedStoredSessionId && focusedStoredSessionLinkedCodex)

  // The stored id is the owner identity even after the route has resumed a
  // linked Codex thread into a renderer runtime. Keep the picker on the fenced
  // owner mutation path for every linked row; a runtime id would select the
  // legacy cwd setter and can miss the native writer lock.
  const targetId = linkedStoredSession
    ? focusedStoredSessionId
    : primaryFocused ? undefined : focusedRuntimeId || null

  const changeWorkspace = useCallback(async () => {
    const [dir] = await selectDesktopPaths({ defaultPath: currentCwd, directories: true, multiple: false, title })

    if (dir) {
      if (linkedStoredSession) {
        await changeSessionCwd(dir, targetId ?? undefined, {
          kind: 'stored-linked-codex',
          owner: workspaceOwner,
          runtimeId: hasRuntime ? (primaryFocused ? primaryRuntimeId : focusedRuntimeId) : undefined
        })
      } else {
        await changeSessionCwd(dir, targetId ?? undefined)
      }
    }
  }, [
    changeSessionCwd, currentCwd, focusedRuntimeId, hasRuntime, linkedStoredSession,
    primaryFocused, primaryRuntimeId, targetId, title, workspaceOwner
  ])

  return useMemo(
    () => ({
      disabled: busy,
      hidden: focusedWorkspaceOtherGateway ||
        (focusedStoredSessionId ? !hasRuntime && !focusedStoredSessionLinkedCodex : targetId === null),
      id: 'change-workspace-path',
      label: title,
      onSelect: () => void changeWorkspace().catch(error => notifyError(error, copy.desktop.cwdChangeFailed)),
      title: displayPath(currentCwd)
    }),
    [busy, changeWorkspace, copy.desktop.cwdChangeFailed, currentCwd, focusedStoredSessionId,
      focusedStoredSessionLinkedCodex, focusedWorkspaceOtherGateway, hasRuntime, targetId, title]
  )
}
