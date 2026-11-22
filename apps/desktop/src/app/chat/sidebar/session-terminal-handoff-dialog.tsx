import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/ui/copy-button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useI18n } from '@/i18n'
import { type BrowserTerminalHandoffShell, buildBrowserTerminalHandoff } from '@/lib/browser-terminal-handoff'
import { browserBootstrap, type BrowserBootstrap } from '@/lib/browser-transport'
import { loadBrowserTerminalContext, type BrowserTerminalContext } from '@/lib/browser-terminal-context'

interface SessionTerminalHandoffDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  profile: string
  sessionId: string
}

const SHELLS: BrowserTerminalHandoffShell[] = ['login', 'zsh', 'bash']

export function SessionTerminalHandoffDialog({
  open,
  onOpenChange,
  profile,
  sessionId
}: SessionTerminalHandoffDialogProps) {
  const { t } = useI18n()
  const r = t.sidebar.row
  const [shell, setShell] = useState<BrowserTerminalHandoffShell>('login')
  const [context, setContext] = useState<BrowserTerminalContext | null>(null)
  const [contextError, setContextError] = useState(false)

  useEffect(() => {
    if (!open) {
      return
    }

    let current = true
    setContext(null)
    setContextError(false)
    const bootstrap: BrowserBootstrap | null = browserBootstrap()

    if (!bootstrap) {
      setContextError(true)

      return () => {
        current = false
      }
    }

    void loadBrowserTerminalContext(bootstrap)
      .then(result => {
        if (!current) {
          return
        }

        setContext(result)
        setShell(currentShell =>
          (currentShell === 'zsh' && !result.shells.zsh) || (currentShell === 'bash' && !result.shells.bash)
            ? 'login'
            : currentShell
        )
      })
      .catch(() => {
        if (current) {
          setContextError(true)
        }
      })

    return () => {
      current = false
    }
  }, [open])

  const selectedShellAvailable = shell === 'login' || Boolean(context?.shells[shell])

  const command =
    context && selectedShellAvailable
      ? buildBrowserTerminalHandoff({ hermesHome: context.hermes_home, profile, sessionId, shell })
      : null

  const availableShells = SHELLS.filter(value => value === 'login' || Boolean(context?.shells[value]))

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{r.terminalHandoffTitle}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-sm text-muted-foreground">
          <p>{r.terminalHandoffInstructions}</p>
          <p className="rounded-md border bg-muted/40 p-3 text-foreground">{r.terminalHandoffOwnerWarning}</p>
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium" htmlFor="session-terminal-handoff-shell">
            {r.terminalHandoffShellLabel}
          </label>
          <Select
            disabled={!context}
            onValueChange={value => {
              if (availableShells.includes(value as BrowserTerminalHandoffShell)) {
                setShell(value as BrowserTerminalHandoffShell)
              }
            }}
            value={shell}
          >
            <SelectTrigger className="w-full" id="session-terminal-handoff-shell">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {availableShells.map(value => (
                <SelectItem key={value} value={value}>
                  {value === 'login'
                    ? r.terminalHandoffLoginShell
                    : value === 'zsh'
                      ? r.terminalHandoffZsh
                      : r.terminalHandoffBash}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">{r.terminalHandoffShellNote}</p>
          <p className="text-xs text-muted-foreground">{r.terminalHandoffIdentityNote}</p>
          <p className="text-xs text-muted-foreground">{r.terminalHandoffPathNote}</p>
        </div>
        {command ? (
          <pre className="max-h-32 overflow-auto rounded-md border bg-muted/40 p-3 text-xs text-foreground">
            <code className="select-all break-all font-mono">{command}</code>
          </pre>
        ) : (
          <p aria-live="polite" className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
            {contextError
              ? r.terminalHandoffLoadFailed
              : context && !selectedShellAvailable
                ? r.terminalHandoffShellNote
                : context
                  ? r.terminalHandoffHomeUnsupported
                  : r.terminalHandoffLoading}
          </p>
        )}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} type="button" variant="ghost">
            {t.common.close}
          </Button>
          <CopyButton appearance="button" disabled={!command} label={r.terminalHandoffCopy} text={command ?? ''} />
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
