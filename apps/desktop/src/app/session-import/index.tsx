import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { SearchField } from '@/components/ui/search-field'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { setSessionOwnerHint } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'

import { OverlayView } from '../overlays/overlay-view'
import {
  SESSION_IMPORT_CLAUDE_ROUTE,
  SESSION_IMPORT_CODEX_ROUTE,
  SESSION_IMPORT_MISTRAL_ROUTE,
  SESSION_IMPORT_ROUTE
} from '../routes'

import { type ForeignImportResult, type ForeignPage, type ForeignPreview, foreignRequest } from './api'
import { agentDisplayName, linkBlockedReason } from './capability'
import { capabilityHost, capabilityReasonText } from './capability-copy'
import { CodexThreadView } from './codex-thread-view'
import { ForeignThreadView } from './foreign-thread-view'
import { HostCapabilityStrip } from './host-capability-strip'
import { SnapshotPreview } from './snapshot-preview'
import { useHostCapability } from './use-host-capability'

interface SessionImportViewProps {
  owner: SessionOwnerRoute
  onClose: () => void
  onOpenSession: (id: string) => void
}

export function SessionImportView({ owner, onClose, onOpenSession }: SessionImportViewProps) {
  const { search } = useLocation()
  const navigate = useNavigate()
  const view = new URLSearchParams(search).get('view')
  const back = () => navigate(SESSION_IMPORT_ROUTE, { replace: true })

  if (view === 'codex') {
    return (
      <CodexThreadView
        onBack={back}
        onClose={onClose}
        onOpenSession={onOpenSession}
        owner={owner}
      />
    )
  }

  if (view === 'claude' || view === 'mistral') {
    return (
      <ForeignThreadView agent={view} onBack={back} onClose={onClose} onOpenSession={onOpenSession} owner={owner} />
    )
  }

  return (
    <SnapshotImportView
      onClose={onClose}
      onOpenClaude={() => navigate(SESSION_IMPORT_CLAUDE_ROUTE)}
      onOpenCodex={() => navigate(SESSION_IMPORT_CODEX_ROUTE)}
      onOpenMistral={() => navigate(SESSION_IMPORT_MISTRAL_ROUTE)}
      onOpenSession={onOpenSession}
      owner={owner}
    />
  )
}

function SnapshotImportView({
  owner,
  onClose,
  onOpenSession,
  onOpenClaude,
  onOpenCodex,
  onOpenMistral
}: SessionImportViewProps & {
  onOpenClaude: () => void
  onOpenCodex: () => void
  onOpenMistral: () => void
}) {
  const { t, locale } = useI18n()
  const copy = t.sessionImport
  const queryClient = useQueryClient()
  const [source, setSource] = useState<'all' | 'claude' | 'codex'>('all')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const lifetime = useRef<AbortController | null>(null)
  // eslint-disable-next-line no-restricted-syntax -- lifetime cancellation, not a mirrored reactive value
  useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller

    return () => controller.abort()
  }, [])
  const scope = [owner.connectionId, owner.profile]

  const sessions = useInfiniteQuery({
    queryKey: ['foreign-sessions', ...scope, source],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      foreignRequest<ForeignPage>(
        owner,
        'list',
        {
          source: source === 'all' ? null : source,
          offset: pageParam
        },
        signal
      ),
    getNextPageParam: page => page.next_offset,
    retry: false
  })

  const rows = [
    ...new Map((sessions.data?.pages.flatMap(page => page.sessions) ?? []).map(row => [row.id, row])).values()
  ]

  const visible = rows.filter(row =>
    `${row.title} ${row.cwd ?? ''} ${row.excerpt}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())
  )

  const current = rows.find(row => row.id === selected)

  const preview = useQuery({
    queryKey: ['foreign-preview', ...scope, selected],
    queryFn: ({ signal }) => foreignRequest<ForeignPreview>(owner, 'preview', { id: selected }, signal),
    enabled: Boolean(current),
    retry: false
  })

  const host = sessions.data?.pages[0]?.host
  const unreadable = sessions.data?.pages.reduce((sum, page) => sum + page.unreadable, 0) ?? 0

  // What the CONNECTED gateway can actually do with each native store. Before the first answer —
  // or against a gateway that predates `session.foreign.status` — nothing is blocked, so the
  // picker behaves exactly as it did before this capability existed.
  const capability = useHostCapability(owner)
  const blockers = (['codex', 'claude', 'mistral'] as const).map(agent => ({
    agent,
    reason: linkBlockedReason(capability, agent)
  }))
  const blockedByAgent = new Map(blockers.map(row => [row.agent, row.reason]))
  const blockedNote = (agent: (typeof blockers)[number]['agent'], hostLabel: string | null | undefined) =>
    capabilityReasonText(
      copy,
      linkBlockedReason(capability, agent),
      agentDisplayName(agent),
      capabilityHost(capability, hostLabel || copy.connectedComputer)
    )

  async function continueSession() {
    if (!current || pending) {
      return
    }

    const signal = lifetime.current?.signal
    setPending(true)
    setError('')

    try {
      const result = await foreignRequest<ForeignImportResult>(owner, 'import', { id: current.id }, signal)
      setSessionOwnerHint(result.session_id, owner)
      void queryClient.invalidateQueries({ queryKey: ['foreign-preview', ...scope] })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })

      if (!signal?.aborted) {
        onOpenSession(result.session_id)
      }
    } catch (cause) {
      if (!signal?.aborted) {
        setError(cause instanceof Error ? cause.message : copy.importError)
      }
    } finally {
      if (!signal?.aborted) {
        setPending(false)
      }
    }
  }

  return (
    <OverlayView
      onClose={onClose}
      rootClassName="bg-(--ui-bg-elevated)"
      titlebarActions={
        <Button
          aria-label={t.common.refresh}
          disabled={sessions.isFetching}
          onClick={() => {
            void sessions.refetch()

            if (current) {
              void preview.refetch()
            }
          }}
          size="icon-titlebar"
          variant="ghost"
        >
          <Codicon name="refresh" />
        </Button>
      }
    >
      <section aria-label={copy.title} className="session-import flex h-full min-h-0 flex-col">
        <header className="px-8 pb-6 pt-12">
          <h1 className="text-3xl font-semibold tracking-tight text-(--ui-text-primary)">{copy.title}</h1>
          <p className="mt-2 text-sm text-(--ui-text-secondary)">{copy.subtitle}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              disabled={Boolean(blockedByAgent.get('codex'))}
              onClick={onOpenCodex}
              title={blockedByAgent.get('codex') ? blockedNote('codex', host) : undefined}
              variant="secondary"
            >
              {copy.codexThreads}
            </Button>
            <Button
              disabled={Boolean(blockedByAgent.get('claude'))}
              onClick={onOpenClaude}
              title={blockedByAgent.get('claude') ? blockedNote('claude', host) : undefined}
              variant="secondary"
            >
              {copy.claudeThreads}
            </Button>
            <Button
              disabled={Boolean(blockedByAgent.get('mistral'))}
              onClick={onOpenMistral}
              title={blockedByAgent.get('mistral') ? blockedNote('mistral', host) : undefined}
              variant="secondary"
            >
              {copy.mistralThreads}
            </Button>
          </div>
          {blockers.some(row => row.reason) && (
            <div className="mt-3 space-y-1 text-xs leading-5 text-(--ui-text-secondary)" role="note">
              {blockers
                .filter(row => row.reason)
                .map(row => (
                  <p data-blocked-agent={row.agent} key={row.agent}>
                    {blockedNote(row.agent, host)}
                  </p>
                ))}
            </div>
          )}
          <div className="mt-5 space-y-2">
            <HostCapabilityStrip capability={capability} detailAgent="all" fallbackHost={host} />
            <span className="inline-flex items-center gap-2 text-xs text-(--ui-text-secondary)">
              <Codicon name="account" />
              {copy.destination} {owner.targetProfile ?? owner.profile}
            </span>
          </div>
        </header>
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(17rem,0.85fr)_minmax(0,1.6fr)] border-t border-(--ui-stroke-tertiary) max-[760px]:grid-cols-1">
          <aside
            className={cn(
              'flex min-h-0 flex-col bg-(--ui-sidebar-surface-background)',
              selected && 'max-[760px]:hidden'
            )}
          >
            <div className="flex flex-col gap-5 px-6 pb-4 pt-6">
              <SegmentedControl
                onChange={value => {
                  setSource(value)
                  setSelected(null)
                  setError('')
                }}
                options={[
                  { id: 'all', label: copy.all },
                  { id: 'claude', label: 'Claude Code' },
                  { id: 'codex', label: 'Codex' }
                ]}
                value={source}
              />
              {rows.length > 0 && (
                <SearchField
                  aria-label={copy.search}
                  containerClassName="opacity-100"
                  onChange={setSearch}
                  placeholder={copy.search}
                  value={search}
                />
              )}
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
                  <EmptyState
                    description={search ? copy.searchHelp : copy.emptyHelp}
                    title={search ? copy.noMatches : copy.empty}
                  />
                </div>
              )}
              {visible.map((row, index) => {
                const date = new Date(row.mtime * 1000).toLocaleDateString(locale, {
                  month: 'short',
                  day: 'numeric'
                })

                const previous = visible[index - 1]

                const newDate =
                  !previous ||
                  new Date(previous.mtime * 1000).toDateString() !== new Date(row.mtime * 1000).toDateString()

                return (
                  <div key={row.id}>
                    {newDate && <p className="px-3 pb-2 pt-5 text-xs font-medium text-(--ui-text-secondary)">{date}</p>}
                    <button
                      aria-pressed={selected === row.id}
                      className={cn(
                        'w-full cursor-pointer rounded-md px-3 py-4 text-start transition-colors hover:bg-(--chrome-action-hover) focus-visible:outline-2 focus-visible:outline-(--ui-accent)',
                        selected === row.id && 'bg-(--chrome-action-hover)'
                      )}
                      onClick={() => {
                        setSelected(row.id)
                        setError('')
                      }}
                      type="button"
                    >
                      <span className="mb-2 flex items-center gap-2 text-xs text-(--ui-text-secondary)">
                        <Codicon name="comment-discussion" />
                        {row.label}
                        <span className="ms-auto tabular-nums">
                          {row.turn_count} {copy.messages}
                        </span>
                      </span>
                      <span className="line-clamp-2 text-sm font-medium leading-5 text-(--ui-text-primary)">
                        {row.title}
                      </span>
                      <span className="mt-1 block truncate text-xs leading-5 text-(--ui-text-secondary)">
                        {row.cwd || row.excerpt}
                      </span>
                    </button>
                  </div>
                )
              })}
              {unreadable > 0 && <p className="px-3 py-4 text-xs text-(--ui-text-secondary)">{copy.skipped}</p>}
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
          <SnapshotPreview
            current={current}
            error={error}
            onBack={() => setSelected(null)}
            onContinue={() => void continueSession()}
            pending={pending}
            preview={preview}
            selected={selected}
          />
        </div>
      </section>
    </OverlayView>
  )
}
