// Session-import thread picker sidebar (sibling of codex/foreign-thread-view):
// the search + list + "load more" panel both views render identically.

import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { SearchField } from '@/components/ui/search-field'
import { cn } from '@/lib/utils'

interface ThreadRow {
  cwd?: string | null
  id: string
  preview: string
  title: string
}

interface ThreadListPanelProps<Row extends ThreadRow> {
  copy: {
    more: string
    scanError: string
    scanHelp: string
    scanning: string
    search: string
  }
  emptyTitle: string
  hasNextPage: boolean
  isError: boolean
  isFetchingNextPage: boolean
  isPending: boolean
  onFetchNextPage: () => void
  onRefetch: () => void
  onSearch: (next: string) => void
  onSelected: (id: string) => void
  retryLabel: string
  search: string
  selected: string | null
  visible: Row[]
}

export function ThreadListPanel<Row extends ThreadRow>({
  copy,
  emptyTitle,
  hasNextPage,
  isError,
  isFetchingNextPage,
  isPending,
  onFetchNextPage,
  onRefetch,
  onSearch,
  onSelected,
  retryLabel,
  search,
  selected,
  visible
}: ThreadListPanelProps<Row>) {
  return (
    <>
      <div className="px-6 pb-4 pt-6">
        <SearchField
          aria-label={copy.search}
          containerClassName="opacity-100"
          onChange={onSearch}
          placeholder={copy.search}
          value={search}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-5">
        {isPending && <Loader className="mx-auto my-12" label={copy.scanning} />}
        {isError && (
          <ErrorState className="p-5" description={copy.scanHelp} title={copy.scanError}>
            <Button onClick={onRefetch} variant="secondary">
              {retryLabel}
            </Button>
          </ErrorState>
        )}
        {!isPending && !isError && visible.length === 0 && (
          <div className="p-5">
            <EmptyState title={emptyTitle} />
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
            onClick={() => onSelected(row.id)}
            type="button"
          >
            <span className="block line-clamp-2 text-sm font-medium">{row.title || row.preview || row.id}</span>
            <span className="mt-1 block truncate text-xs text-(--ui-text-secondary)">{row.cwd || row.id}</span>
          </button>
        ))}
        {hasNextPage && (
          <div className="flex justify-center pt-4">
            <Button disabled={isFetchingNextPage} onClick={onFetchNextPage} variant="text">
              {copy.more}
            </Button>
          </div>
        )}
      </div>
    </>
  )
}
