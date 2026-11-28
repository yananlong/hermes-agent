import type { UseQueryResult } from '@tanstack/react-query'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { PanelEmpty } from '../overlays/panel'

import type { ForeignPreview, ForeignSession } from './api'

interface SnapshotPreviewProps {
  current: ForeignSession | undefined
  selected: string | null
  preview: UseQueryResult<ForeignPreview>
  pending: boolean
  error: string
  onBack: () => void
  onContinue: () => void
}

export function SnapshotPreview({
  current,
  selected,
  preview,
  pending,
  error,
  onBack,
  onContinue
}: SnapshotPreviewProps) {
  const { t } = useI18n()
  const copy = t.sessionImport

  return (
    <main className={cn('flex min-h-0 min-w-0 flex-col', !selected && 'max-[760px]:hidden')}>
      {!current ? (
        <PanelEmpty description={copy.chooseHelp} icon="comment-discussion" title={copy.choose} />
      ) : (
        <>
          <div className="px-8 pb-5 pt-6">
            <div className="mb-4 hidden max-[760px]:block">
              <Button onClick={() => onBack()} variant="text">
                {t.common.back}
              </Button>
            </div>
            <h2 className="text-xl font-medium leading-7 tracking-tight">{current.title}</h2>
            <p className="mt-2 text-xs text-(--ui-text-secondary)">
              {current.label} · {current.turn_count} {copy.messages}
            </p>
          </div>
          <div aria-live="polite" className="min-h-0 flex-1 overflow-y-auto px-8 pb-8" key={current.id}>
            {preview.isPending && <Loader className="mx-auto my-12" label={copy.previewLoading} />}
            {preview.isError && (
              <ErrorState description={copy.previewHelp} title={copy.previewError}>
                <Button onClick={() => void preview.refetch()} variant="secondary">
                  {t.common.retry}
                </Button>
              </ErrorState>
            )}
            {preview.data && (
              <div className="max-w-prose space-y-7">
                {preview.data.truncated && <p className="text-xs text-(--ui-text-secondary)">{copy.previewLimit}</p>}
                {preview.data.messages.map((message, index) => (
                  <article className="min-w-0" key={index}>
                    <p className="mb-2 text-xs font-semibold text-(--ui-text-secondary)">
                      {message.role === 'user' ? copy.you : current.label}
                    </p>
                    <div className="text-sm leading-6 wrap-anywhere">
                      <MarkdownTextContent isRunning={false} previewOnly text={message.content} />
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>
          <footer className="flex flex-wrap items-center justify-between gap-4 border-t border-(--ui-stroke-tertiary) px-8 py-5">
            <p className="max-w-xs text-xs leading-5 text-(--ui-text-secondary)">
              {preview.data?.already_imported ? copy.snapshot : copy.copyNotice}
            </p>
            <Button disabled={pending || !preview.data || preview.isError} onClick={() => void onContinue()}>
              {pending ? copy.importing : preview.data?.already_imported ? copy.open : copy.continue}
              <Codicon name="arrow-right" />
            </Button>
            {error && (
              <div className="w-full text-sm text-destructive" role="alert">
                {copy.importError} {error}
              </div>
            )}
          </footer>
        </>
      )}
    </main>
  )
}
