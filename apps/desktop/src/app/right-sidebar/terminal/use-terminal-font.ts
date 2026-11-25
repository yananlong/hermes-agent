import { useStore } from '@nanostores/react'
import type { WebglAddon } from '@xterm/addon-webgl'
import type { Terminal } from '@xterm/xterm'
import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

import {
  $terminalFontFamily,
  $terminalFontSize,
  applyTerminalFont,
  normalizeTerminalFontSize,
  resolveTerminalFontFamily
} from './terminal-font'
import { redrawAllTerminals } from './terminals'

interface TerminalFontControllerOptions {
  fitRef: RefObject<(() => void) | null>
  termRef: RefObject<Terminal | null>
  webglRef: RefObject<WebglAddon | null>
}

/**
 * Share profile-backed font state across user and agent terminals. The owner
 * flips mountedRef only after term.open(); before then, its mount path reads
 * the latest refs and warms that family at its configured size.
 */
export function useTerminalFontController({ fitRef, termRef, webglRef }: TerminalFontControllerOptions) {
  const configured = useStore($terminalFontFamily)
  const configuredSize = useStore($terminalFontSize)
  const fontFamily = resolveTerminalFontFamily(configured)
  const fontSize = normalizeTerminalFontSize(configuredSize)
  const latestFontFamilyRef = useRef(fontFamily)
  const latestFontSizeRef = useRef(fontSize)
  const mountedRef = useRef(false)
  const generationRef = useRef(0)

  latestFontFamilyRef.current = fontFamily
  latestFontSizeRef.current = fontSize

  useEffect(() => {
    const term = termRef.current

    if (
      !mountedRef.current ||
      !term ||
      (term.options.fontFamily === fontFamily && term.options.fontSize === fontSize)
    ) {
      return
    }

    const generation = ++generationRef.current
    let cancelled = false

    void applyTerminalFont({
      clearTextureAtlas: () => webglRef.current?.clearTextureAtlas(),
      fit: () => fitRef.current?.(),
      fontFamily,
      fontSize,
      isCurrent: () => !cancelled && generationRef.current === generation,
      term
    }).then(applied => {
      if (applied) {
        // Font is an atlas-share key. Fan out only after the asynchronous warm
        // and this terminal's atlas clear, so sibling models rebuild against
        // the new shared glyph pages. Include this terminal in the redraw:
        // coalesced changes from multiple panes must not skip a cleared atlas.
        redrawAllTerminals()
      }
    })

    return () => {
      cancelled = true
    }
  }, [fitRef, fontFamily, fontSize, termRef, webglRef])

  return { latestFontFamilyRef, latestFontSizeRef, mountedRef }
}
