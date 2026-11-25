import { useEffect, useRef, useState, type RefObject } from "react";
import { api } from "@/lib/api";
import {
  DEFAULT_TERMINAL_APPEARANCE,
  applyTerminalAppearance,
  terminalAppearanceFromConfig,
  terminalFontSizeForWidth,
  terminalTierWidthPx,
  type TerminalAppearance,
} from "@/lib/terminal-appearance";
import type { RemeasurableTerminal } from "@/lib/terminal-font-refit";

const CONFIG_SAVED_EVENT = "hermes-terminal-config-saved";

export function notifyTerminalConfigSaved(profile: string): void {
  window.dispatchEvent(new CustomEvent(CONFIG_SAVED_EVENT, { detail: profile }));
}

/** Read only the selected profile, and discard responses from superseded reads. */
export function useTerminalAppearance(profile: string): TerminalAppearance {
  const [loaded, setLoaded] = useState<{ profile: string; appearance: TerminalAppearance } | null>(null);

  useEffect(() => {
    let active = true;
    let generation = 0;
    const refresh = () => {
      const request = ++generation;
      void api.getConfig(profile).then((config) => {
        if (active && request === generation) {
          const next = terminalAppearanceFromConfig(config);
          const appearance = next.fontFamily === DEFAULT_TERMINAL_APPEARANCE.fontFamily &&
            next.fontSize === DEFAULT_TERMINAL_APPEARANCE.fontSize ? DEFAULT_TERMINAL_APPEARANCE : next;
          setLoaded((previous) => previous?.profile === profile &&
            previous.appearance.fontFamily === appearance.fontFamily && previous.appearance.fontSize === appearance.fontSize
            ? previous : { profile, appearance });
        }
      }).catch(() => {
        // Keep the last successful value if the server is temporarily unavailable.
      });
    };
    const onSaved = (event: Event) => {
      if ((event as CustomEvent<string>).detail === profile) refresh();
    };
    refresh();
    window.addEventListener(CONFIG_SAVED_EVENT, onSaved);
    window.addEventListener("focus", refresh);
    return () => {
      active = false;
      window.removeEventListener(CONFIG_SAVED_EVENT, onSaved);
      window.removeEventListener("focus", refresh);
    };
  }, [profile]);

  return loaded?.profile === profile ? loaded.appearance : DEFAULT_TERMINAL_APPEARANCE;
}

/** Keep a mounted xterm in sync without restarting its socket or PTY. */
export function useEmbeddedTerminalAppearance(
  profile: string,
  termRef: RefObject<(RemeasurableTerminal & { options: { fontFamily?: string; fontSize?: number } }) | null>,
  hostRef: RefObject<HTMLElement | null>,
  fitRef: RefObject<(() => void) | null>,
  responsive = false,
) {
  const appearance = useTerminalAppearance(profile);
  const appearanceRef = useRef(appearance);
  useEffect(() => {
    appearanceRef.current = appearance;
    const term = termRef.current;
    if (!term) return;
    const fontSize = responsive
      ? terminalFontSizeForWidth(terminalTierWidthPx(hostRef.current), appearance.fontSize)
      : appearance.fontSize;
    return applyTerminalAppearance(term, { ...appearance, fontSize }, () => fitRef.current?.());
  }, [appearance, fitRef, hostRef, responsive, termRef]);
  return appearanceRef;
}
