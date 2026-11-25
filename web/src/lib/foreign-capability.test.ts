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
import { noteDashboardConnectionOpened } from "./gateway-connection-generation"

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

it("asks the profile-scoped RPC about the profile it is showing", async () => {
  const calls: Array<{ method: string; params?: Record<string, unknown> }> = []
  const request = async (method: string, params?: Record<string, unknown>) => {
    calls.push({ method, params })
    return status([row("claude")])
  }

  await readHostCapability(request, "work")
  expect(calls[0]).toEqual({ method: "session.foreign.status", params: { profile: "work" } })

  // The launch profile is asked with no param at all: "" is how the dashboard's
  // profile scope spells it, and "default" is the gateway's reserved alias for
  // the same home. Sending either as a param would be a scope the RPC resolves
  // to the launch profile anyway, so omitting it is the honest wire shape.
  await readHostCapability(request, "")
  expect(calls[1]?.params).toEqual({})

  await readHostCapability(request, "default")
  expect(calls[2]?.params).toEqual({})
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

it("drops the cached verdict when the dashboard reconnects", async () => {
  let calls = 0
  const request = async () => {
    calls += 1
    throw methodNotFound()
  }

  await readHostCapability(request, "scope-e")
  await readHostCapability(request, "scope-e")
  expect(calls).toBe(1)

  // A socket opened: the serving gateway may be a new build, so the -32601
  // verdict from the previous connection may no longer hold.
  noteDashboardConnectionOpened()

  await readHostCapability(request, "scope-e")
  expect(calls).toBe(2)
})

it("asks what the profile can do and forwards the profile to the RPC", async () => {
  let captured: Record<string, unknown> | undefined
  const request = async (_method: string, params?: Record<string, unknown>) => {
    captured = params
    return status([row("claude")])
  }

  await readHostCapability(request, "work")

  expect(captured).toEqual({ profile: "work" })
})

it("recognizes method-not-found by code or by the wrapped message", () => {
  expect(isMissingCapabilityMethod({ code: -32601 })).toBe(true)
  expect(isMissingCapabilityMethod(new Error("no such method: session.foreign.status"))).toBe(true)
  expect(isMissingCapabilityMethod({ code: 500 })).toBe(false)
})
