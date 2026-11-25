// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { useProfileWorkspace } from "./useProfileWorkspace";

it("remembers each profile's workspace for the next chat", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const values = new Map<string, string>([["hermes-chat-workspace:b", "/repo-b"]]);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  function Harness({ profile }: { profile: string }) {
    const [workspace, select] = useProfileWorkspace(profile);
    return <button onClick={() => select("/repo-a")}>{workspace || "none"}</button>;
  }
  try {
    await act(async () => root.render(<Harness profile="a" />));
    await act(async () => container.querySelector("button")!.click());
    expect(container.textContent).toBe("/repo-a");
    await act(async () => root.render(<Harness profile="b" />));
    expect(container.textContent).toBe("/repo-b");
    await act(async () => root.render(<Harness profile="a" />));
    expect(container.textContent).toBe("/repo-a");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
