// @vitest-environment jsdom
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { notifyTerminalConfigSaved, useEmbeddedTerminalAppearance } from "./useTerminalAppearance";

const mocks = vi.hoisted(() => ({ getConfig: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: mocks }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function makeTerminal() {
  return {
    options: { fontFamily: "", fontSize: 15 },
    rows: 24,
    clearTextureAtlas: vi.fn(),
    refresh: vi.fn(),
  };
}

function Harness({ profile, term }: { profile: string; term: ReturnType<typeof makeTerminal> }) {
  const termRef = useRef(term);
  const hostRef = useRef<HTMLDivElement>(null);
  const fitRef = useRef<(() => void) | null>(() => undefined);
  useEmbeddedTerminalAppearance(profile, termRef, hostRef, fitRef);
  return <div ref={hostRef} />;
}

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  mocks.getConfig.mockReset();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

it("keeps profile responses isolated and updates the existing terminal after a save", async () => {
  const slow = deferred<Record<string, unknown>>();
  mocks.getConfig.mockImplementation((profile: string) => profile === "old"
    ? slow.promise : Promise.resolve({ terminal: { font_family: "Hack", font_size: 23 } }));
  const term = makeTerminal();
  await act(async () => root.render(<Harness profile="old" term={term} />));
  await act(async () => root.render(<Harness profile="work" term={term} />));
  expect(term.options.fontFamily).toContain("'Hack'");
  expect(term.options.fontSize).toBe(23);

  await act(async () => { slow.resolve({ terminal: { font_family: "Stale", font_size: 9 } }); });
  expect(term.options.fontFamily).toContain("'Hack'");
  expect(term.options.fontSize).toBe(23);

  mocks.getConfig.mockResolvedValue({ terminal: { font_family: "Meslo", font_size: 18 } });
  await act(async () => { notifyTerminalConfigSaved("old"); });
  expect(term.options.fontSize).toBe(23);
  await act(async () => { notifyTerminalConfigSaved("work"); });
  expect(term.options.fontFamily).toContain("'Meslo'");
  expect(term.options.fontSize).toBe(18);
  expect(term.clearTextureAtlas).toHaveBeenCalled();
  expect(term.refresh).toHaveBeenCalledWith(0, 23);

  const staleRefresh = deferred<Record<string, unknown>>();
  mocks.getConfig.mockReturnValueOnce(staleRefresh.promise)
    .mockResolvedValueOnce({ terminal: { font_family: "Newest", font_size: 26 } });
  await act(async () => { notifyTerminalConfigSaved("work"); });
  await act(async () => { notifyTerminalConfigSaved("work"); });
  await act(async () => { staleRefresh.resolve({ terminal: { font_family: "Stale", font_size: 10 } }); });
  expect(term.options.fontFamily).toContain("'Newest'");
  expect(term.options.fontSize).toBe(26);
});
