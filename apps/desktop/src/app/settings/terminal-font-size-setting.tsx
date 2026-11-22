import { useEffect, useRef, useState } from 'react'

import {
  DEFAULT_TERMINAL_FONT_SIZE,
  MAX_TERMINAL_FONT_SIZE,
  MIN_TERMINAL_FONT_SIZE,
  normalizeTerminalFontSize,
  setTerminalFontSizeFromConfig
} from '@/app/right-sidebar/terminal/terminal-font'
import { Button } from '@/components/ui/button'
import { Slider } from '@/components/ui/slider'
import { saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { notifyError } from '@/store/notifications'
import type { HermesConfigRecord } from '@/types/hermes'

import { setHermesConfigCache, useHermesConfigRecord } from '../hooks/use-config-record'
import { useOnProfileSwitch } from '../hooks/use-on-profile-switch'
import { useProfileSwitchLatch } from '../hooks/use-profile-switch-latch'

import { getNested, setNested } from './helpers'
import { ListRow } from './primitives'

const AUTOSAVE_DELAY_MS = 550
const CONFIG_PATH = 'terminal.font_size'

function fontSizeFromConfig(config: HermesConfigRecord): number {
  return normalizeTerminalFontSize(getNested(config, CONFIG_PATH))
}

/** Profile-backed size control for both interactive and agent terminal panes. */
export function TerminalFontSizeSetting() {
  const { t } = useI18n()
  const copy = t.settings.appearance
  const { data: loadedConfig, dataUpdatedAt, writeScope } = useHermesConfigRecord()
  const [draft, setDraft] = useState<number | null>(null)
  // Keep the prior profile's value out of view until its config fetch completes.
  const { arm: armProfileLatch, pending: profilePending } = useProfileSwitchLatch({ dataUpdatedAt })
  const [saveVersion, setSaveVersion] = useState(0)
  const saveVersionRef = useRef(0)

  // Lexically outside the effect so a save callback can cancel its version
  // without writing to the ref from an effect body.
  const cancelPendingSave = () => {
    saveVersionRef.current = 0
  }

  useEffect(() => {
    if (!loadedConfig || draft !== null || profilePending) {
      return
    }

    const value = fontSizeFromConfig(loadedConfig)
    setDraft(value)
    setTerminalFontSizeFromConfig(value)
  }, [draft, loadedConfig, profilePending])

  useOnProfileSwitch(() => {
    saveVersionRef.current += 1
    setDraft(null)
    armProfileLatch()
    setSaveVersion(0)
    setTerminalFontSizeFromConfig(DEFAULT_TERMINAL_FONT_SIZE)
  })

  useEffect(() => {
    if (draft === null || saveVersion === 0 || !loadedConfig) {
      return
    }

    const version = saveVersion
    const value = normalizeTerminalFontSize(draft)

    if (value === fontSizeFromConfig(loadedConfig)) {
      return
    }

    const rollback = fontSizeFromConfig(loadedConfig)

    const timeout = window.setTimeout(() => {
      const next = setNested(loadedConfig, CONFIG_PATH, value)

      // Sparse patch: config writes deep-merge, so unrelated settings survive.
      void saveHermesConfig(setNested({}, CONFIG_PATH, value), writeScope)
        .then(result => {
          if (!result.ok) {
            throw new Error(t.settings.config.autosaveFailed)
          }

          if (saveVersionRef.current !== version) {
            return
          }

          setHermesConfigCache(previous => (previous ? setNested(previous, CONFIG_PATH, value) : next))
        })
        .catch(error => {
          if (saveVersionRef.current !== version) {
            return
          }

          cancelPendingSave()
          setSaveVersion(0)
          setDraft(rollback)
          setTerminalFontSizeFromConfig(rollback)
          notifyError(error, t.settings.config.autosaveFailed)
        })
    }, AUTOSAVE_DELAY_MS)

    return () => window.clearTimeout(timeout)
  }, [draft, loadedConfig, saveVersion, t.settings.config.autosaveFailed, writeScope])

  const update = (value: number) => {
    saveVersionRef.current += 1
    setDraft(value)
    setSaveVersion(saveVersionRef.current)
    setTerminalFontSizeFromConfig(value)
  }

  const value = draft ?? DEFAULT_TERMINAL_FONT_SIZE

  return (
    <ListRow
      action={
        <div className="flex min-w-0 items-center gap-3">
          <Slider
            aria-label={copy.terminalFontSizeTitle}
            aria-valuetext={`${value} px`}
            disabled={draft === null}
            max={MAX_TERMINAL_FONT_SIZE}
            min={MIN_TERMINAL_FONT_SIZE}
            onChange={event => {
              triggerHaptic('selection')
              update(Number(event.target.value))
            }}
            step={1}
            value={value}
          />
          <output className="w-12 text-right text-[length:var(--conversation-caption-font-size)] tabular-nums text-(--ui-text-tertiary)">
            {value} px
          </output>
          <Button
            disabled={draft === null || value === DEFAULT_TERMINAL_FONT_SIZE}
            onClick={() => update(DEFAULT_TERMINAL_FONT_SIZE)}
            size="inline"
            variant="text"
          >
            {copy.terminalFontSizeReset}
          </Button>
        </div>
      }
      description={copy.terminalFontSizeDesc}
      title={copy.terminalFontSizeTitle}
    />
  )
}
