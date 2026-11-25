// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("@/lib/byot", () => ({
  listByotSessions: async () => [{ id: "abc", name: "iTerm", shell: "zsh", cwd: "/tmp" }],
}));
import { ByotTerminalControls } from "./ByotTerminalControls";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { document.body.innerHTML = ""; });

it("offers attach while connected and explicit detach after attachment", async () => {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const select = vi.fn();
  await act(async () => { root.render(<ByotTerminalControls isActive currentId={null} onSelect={select} />); });
  const attach = host.querySelector<HTMLButtonElement>('button[aria-label="Attach external terminal iTerm"]');
  expect(attach).not.toBeNull();
  await act(async () => { attach!.click(); });
  expect(select).toHaveBeenCalledWith("abc");
  await act(async () => { root.render(<ByotTerminalControls isActive currentId="abc" onSelect={select} />); });
  const detach = host.querySelector<HTMLButtonElement>('button[aria-label="Detach external terminal and use the embedded one"]');
  expect(detach).not.toBeNull();
  await act(async () => { detach!.click(); });
  expect(select).toHaveBeenLastCalledWith(null);
  await act(async () => { root.unmount(); });
});
