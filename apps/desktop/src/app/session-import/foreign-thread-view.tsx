import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { SearchField } from '@/components/ui/search-field'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { setSessionOwnerHint } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'

import { OverlayView } from '../overlays/overlay-view'
import { PanelEmpty } from '../overlays/panel'

import { type LinkedAgent, type LinkedLinkResult, type LinkedThreadPage, linkedThreadRequest } from './api'
import { agentDisplayName, continueReason, linkBlockedReason } from './capability'
import { capabilityHost, capabilityReasonText } from './capability-copy'
import { HostCapabilityStrip } from './host-capability-strip'
import { useHostCapability } from './use-host-capability'

interface ForeignThreadViewProps {
  agent: LinkedAgent
  owner: SessionOwnerRoute
  onBack: () => void
  onClose: () => void
  onOpenSession: (id: string) => void
}

export function ForeignThreadView({ agent, owner, onBack, onClose, onOpenSession }: ForeignThreadViewProps) {
  const { t } = useI18n()
  const copy = t.sessionImport
  const agentCopy = agent === 'claude' ? { title: copy.claudeThreads, help: copy.claudeThreadsHelp }
    : { title: copy.mistralThreads, help: copy.mistralThreadsHelp }
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const lifetime = useRef<AbortController | null>(null)
  // eslint-disable-next-line no-restricted-syntax -- lifetime cancellation, not a mirrored reactive value
  useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller

    return () => controller.abort()
  }, [])

  const sessions = useInfiniteQuery({
    queryKey: [`${agent}-threads`, owner.connectionId, owner.profile],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      linkedThreadRequest<LinkedThreadPage>(owner, agent, 'list', { cursor: pageParam, limit: 25 }, signal),
    getNextPageParam: page => page.next_cursor,
    retry: false
  })

  const rows = [
    ...new Map((sessions.data?.pages.flatMap(page => page.threads) ?? []).map(row => [row.id, row])).values()
  ]

  const visible = rows.filter(row =>
    `${row.title} ${row.cwd ?? ''} ${row.preview} ${row.id}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())
  )

  const current = rows.find(row => row.id === selected)
  // Same contract as the Codex view: only a capability answer that says the host cannot serve
  // this store disables the link; `unknown` keeps today's behaviour, and the read-only note
  // stays unless the host confirms a send would append to the native thread.
  const capability = useHostCapability(owner)
  const linkBlocked = linkBlockedReason(capability, agent)
  const writable = continueReason(capability, agent) === null
  const blockedText = linkBlocked
    ? capabilityReasonText(copy, linkBlocked, agentDisplayName(agent), capabilityHost(capability, null))
    : ''

  async function openThread() {
    if (!current || pending) {
      return
    }

    const signal = lifetime.current?.signal
    setPending(true)
    setError('')

    try {
      const result = await linkedThreadRequest<LinkedLinkResult>(
        owner,
        agent,
        'link',
        { session_id: current.id },
        signal
      )

      if (!signal?.aborted) {
        setSessionOwnerHint(result.session_id, owner)
        void queryClient.invalidateQueries({ queryKey: ['sessions'] })
        onOpenSession(result.session_id)
      }
    } catch (cause) {
      if (!signal?.aborted) {
        setError(cause instanceof Error ? cause.message : copy.linkedLinkError)
      }
    } finally {
      if (!signal?.aborted) {
        setPending(false)
      }
    }
  }

  return (
    <OverlayView onClose={onClose} rootClassName="bg-(--ui-bg-elevated)">
      <section aria-label={agentCopy.title} className="session-import flex h-full min-h-0 flex-col">
        <header className="px-8 pb-6 pt-12">
          <Button onClick={onBack} variant="text">
            {t.common.back}
          </Button>
          <h1 className="mt-3 text-3xl font-semibold tracking-tight text-(--ui-text-primary)">{agentCopy.title}</h1>
          <p className="mt-2 text-sm text-(--ui-text-secondary)">{agentCopy.help}</p>
          <div className="mt-4">
            <HostCapabilityStrip capability={capability} />
          </div>
        </header>
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(17rem,0.85fr)_minmax(0,1.6fr)] border-t border-(--ui-stroke-tertiary) max-[760px]:grid-cols-1">
          <aside
            className={cn(
              'flex min-h-0 flex-col bg-(--ui-sidebar-surface-background)',
              selected && 'max-[760px]:hidden'
            )}
          >
            <div className="px-6 pb-4 pt-6">
              <SearchField
                aria-label={copy.search}
                containerClassName="opacity-100"
                onChange={setSearch}
                placeholder={copy.search}
                value={search}
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-5">
              {sessions.isPending && <Loader className="mx-auto my-12" label={copy.scanning} />}
              {sessions.isError && (
                <ErrorState className="p-5" description={copy.scanHelp} title={copy.scanError}>
                  <Button onClick={() => void sessions.refetch()} variant="secondary">
                    {t.common.retry}
                  </Button>
                </ErrorState>
              )}
              {!sessions.isPending && !sessions.isError && visible.length === 0 && (
                <div className="p-5">
                  <EmptyState title={search ? copy.noMatches : copy.linkedNoThreads} />
                </div>
              )}
              {visible.map(row => (
                <button
                  aria-pressed={selected === row.id}
                  className={cn(
                    'w-full cursor-pointer rounded-md px-3 py-4 text-start hover:bg-(--chrome-action-hover)',
                    selected === row.id && 'bg-(--chrome-action-hover)'
                  )}
                  key={row.id}
                  onClick={() => {
                    setSelected(row.id)
                    setError('')
                  }}
                  type="button"
                >
                  <span className="block line-clamp-2 text-sm font-medium">{row.title || row.preview || row.id}</span>
                  <span className="mt-1 block truncate text-xs text-(--ui-text-secondary)">{row.cwd || row.id}</span>
                </button>
              ))}
              {sessions.hasNextPage && (
                <div className="flex justify-center pt-4">
                  <Button
                    disabled={sessions.isFetchingNextPage}
                    onClick={() => void sessions.fetchNextPage()}
                    variant="text"
                  >
                    {copy.more}
                  </Button>
                </div>
              )}
            </div>
          </aside>
          <main className={cn('flex min-h-0 min-w-0 flex-col', !selected && 'max-[760px]:hidden')}>
            {!current ? (
              <PanelEmpty description={copy.chooseHelp} icon="comment-discussion" title={copy.choose} />
            ) : (
              <>
                <div className="flex-1 px-8 py-6">
                  <div className="mb-4 hidden max-[760px]:block">
                    <Button onClick={() => setSelected(null)} variant="text">
                      {t.common.back}
                    </Button>
                  </div>
                  <h2 className="text-xl font-medium">{current.title || current.preview || current.id}</h2>
                  {current.cwd && <p className="mt-2 break-all text-sm text-(--ui-text-secondary)">{current.cwd}</p>}
                  <p className="mt-2 break-all text-xs text-(--ui-text-secondary)">{current.id}</p>
                  {current.preview && <p className="mt-5 whitespace-pre-wrap text-sm">{current.preview}</p>}
                  <p
                    className="mt-5 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) px-3 py-2.5 text-xs leading-5 text-(--ui-text-secondary)"
                    role="note"
                  >
                    {writable ? copy.hostCapabilityWritable(agentDisplayName(agent)) : copy.linkedReadonlyWarning}
                  </p>
                  {blockedText && (
                    <p className="mt-2 text-xs leading-5 text-destructive" data-capability-blocked={agent}>
                      {blockedText}
                    </p>
                  )}
                </div>
                <footer className="flex flex-wrap items-center justify-between gap-4 border-t border-(--ui-stroke-tertiary) px-8 py-5">
                  <p className="max-w-xs text-xs leading-5 text-(--ui-text-secondary)">{copy.linkedHandoff}</p>
                  <Button
                    disabled={pending || Boolean(linkBlocked)}
                    onClick={() => void openThread()}
                    title={blockedText || undefined}
                  >
                    {pending ? copy.linkedLinking : copy.linkedLink}
                    <Codicon name="arrow-right" />
                  </Button>
                  {error && (
                    <div className="w-full text-sm text-destructive" role="alert">
                      {copy.linkedLinkError} {error}
                    </div>
                  )}
                </footer>
              </>
            )}
          </main>
        </div>
      </section>
    </OverlayView>
  )
}
