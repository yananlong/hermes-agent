// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  buildWsUrl: vi.fn(async () => "ws://localhost/api/console"),
}));
vi.mock("@/lib/api", () => ({ api: mocks }));
vi.mock("@/contexts/useProfileScope", () => ({ useProfileScope: () => ({ profile: "work" }) }));
vi.mock("@/hooks/useModalBehavior", () => ({ useModalBehavior: () => ({ current: null }) }));
vi.mock("@/themes", () => { const theme = {}; return { useTheme: () => ({ theme }) }; });
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/addon-unicode11", () => ({ Unicode11Addon: class {} }));
vi.mock("@xterm/addon-web-links", () => ({ WebLinksAddon: class {} }));

const terminals = vi.hoisted(() => [] as Array<{ options: Record<string, unknown> }>);
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    options: Record<string, unknown>;
    rows = 24;
    unicode = { activeVersion: "" };
    constructor(options: Record<string, unknown>) { this.options = options; terminals.push(this); }
    loadAddon() {}
    open() {}
    focus() {}
    dispose() {}
    write() {}
    refresh() {}
    clearTextureAtlas() {}
    onData() { return { dispose() {} }; }
  },
}));

import { notifyTerminalConfigSaved } from "@/hooks/useTerminalAppearance";
import { HermesConsoleModal } from "./HermesConsoleModal";

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = Socket.OPEN;
  constructor() { Socket.instances.push(this); }
  close() {}
  send() {}
}

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  terminals.length = 0;
  Socket.instances = [];
  mocks.getConfig.mockReset().mockResolvedValue({ terminal: { font_family: "Meslo", font_size: 23 } });
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", () => 1);
  vi.stubGlobal("cancelAnimationFrame", () => {});
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it("uses the selected profile's font and applies saved changes without reopening the console", async () => {
  await act(async () => root.render(<HermesConsoleModal open onClose={() => undefined} />));
  const term = terminals[0];
  expect(mocks.getConfig).toHaveBeenCalledWith("work");
  expect(term.options.fontFamily).toContain("'Meslo'");
  expect(term.options.fontSize).toBe(23);
  const socket = Socket.instances[0];

  mocks.getConfig.mockResolvedValue({ terminal: { font_family: "Hack", font_size: 19 } });
  await act(async () => { notifyTerminalConfigSaved("work"); });
  expect(term.options.fontFamily).toContain("'Hack'");
  expect(term.options.fontSize).toBe(19);
  expect(terminals).toEqual([term]);
  expect(Socket.instances).toEqual([socket]);
});
