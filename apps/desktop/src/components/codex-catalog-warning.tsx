import type { ModelOptionProvider } from '@hermes/shared'

/** Shared warning for a Codex catalog row that could not be verified live. */
export function CodexCatalogWarning({
  className,
  hidden = false,
  provider
}: {
  className: string
  hidden?: boolean
  provider?: ModelOptionProvider
}) {
  if (hidden || provider?.source !== 'codex-app-server' || provider.authenticated !== false || !provider.warning) {
    return null
  }

  return (
    <p className={`text-xs text-muted-foreground ${className}`} role="status">
      {provider.warning}
    </p>
  )
}
