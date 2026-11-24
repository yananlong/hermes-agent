import { afterEach, expect, it } from "vitest"

import {
  agentDisplayName,
  agentHostCapability,
  capabilityHost,
  type ForeignAgentCapability,
  type ForeignStatus,
  isHostWritable,
  isMissingCapabilityMethod,
  readHostCapability,
  resetHostCapabilityCache,
  storeBlockedReason,
} from "./foreign-capability"

afterEach(() => {
  resetHostCapabilityCache()
})

const methodNotFound = () => Object.assign(new Error("unknown method"), { code: -32601 })

function row(agent: "codex" | "claude" | "mistral", overrides: Partial<ForeignAgentCapability> = {}): ForeignAgentCapability {
  return {
    agent,
    label: agent,
    store: { available: true, readable: true, reason_code: null, reason: null },
    cli: { available: true, command: agent, path: null, version: null, source: "path", reason_code: null, reason: null },
    continue_status: { supported: true, writable: false, reason_code: "send_read_only", reason: "read-only" },
    ...overrides,
  }
}

const status = (agents: ForeignAgentCapability[]): ForeignStatus => ({ host: "studio", agents })

it("treats a gateway without the method as unknown, never as writable", async () => {
  const capability = await readHostCapability(async () => {
    throw methodNotFound()
  }, "scope-a")

  expect(capability).toEqual({ state: "unknown" })
  expect(isHostWritable(capability, "claude")).toBe(false)
  // Unknown blocks nothing: the dashboard keeps the copy it already had.
  expect(storeBlockedReason(capability, "claude")).toBeNull()
})

it("caches the method-not-found verdict per serving scope", async () => {
  let calls = 0
  const request = async () => {
    calls += 1
    throw methodNotFound()
  }

  await readHostCapability(request, "scope-b")
  await readHostCapability(request, "scope-b")
  expect(calls).toBe(1)

  await readHostCapability(request, "scope-c")
  expect(calls).toBe(2)
})

it("does not cache a broken socket", async () => {
  const failed = await readHostCapability(async () => {
    throw new Error("socket closed")
  }, "scope-d")
  expect(failed).toEqual({ state: "error", message: "socket closed" })

  const ok = await readHostCapability(async () => status([row("claude")]), "scope-d")
  expect(ok.state).toBe("ready")
})

it("reports a missing store per agent and a writable verdict only when explicit", async () => {
  const capability = {
    state: "ready" as const,
    status: status([
      row("claude", {
        store: { available: false, readable: false, reason_code: "store_missing", reason: "none" },
      }),
      row("codex", { continue_status: { supported: true, writable: true, reason_code: null, reason: null } }),
    ]),
  }

  expect(storeBlockedReason(capability, "claude")).toBe("store_missing")
  expect(storeBlockedReason(capability, "codex")).toBeNull()
  expect(isHostWritable(capability, "codex")).toBe(true)
  expect(isHostWritable(capability, "mistral")).toBe(false)
  expect(capabilityHost(capability)).toBe("studio")
  expect(agentHostCapability(capability, "codex")?.agent).toBe("codex")
  expect(agentDisplayName("mistral")).toBe("Mistral Vibe")
})

it("recognizes method-not-found by code or by the wrapped message", () => {
  expect(isMissingCapabilityMethod({ code: -32601 })).toBe(true)
  expect(isMissingCapabilityMethod(new Error("no such method: session.foreign.status"))).toBe(true)
  expect(isMissingCapabilityMethod({ code: 500 })).toBe(false)
})
