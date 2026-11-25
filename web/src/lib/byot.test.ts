import { describe, expect, it } from "vitest";

import { parseByotControlMessage, withByotTarget } from "@/lib/byot";

describe("parseByotControlMessage", () => {
  it("parses a byot control frame", () => {
    expect(
      parseByotControlMessage('{"type":"byot","id":"abc123","name":"iterm-zsh","viewers":1}'),
    ).toMatchObject({ id: "abc123", name: "iterm-zsh" });
  });

  it("ignores resume control frames, plain text and non-JSON output", () => {
    expect(parseByotControlMessage('{"type":"resume","id":"sess-1"}')).toBeNull();
    expect(parseByotControlMessage('{"type":"byot"}')).toBeNull();
    expect(parseByotControlMessage("hello from zsh\r\n")).toBeNull();
    expect(parseByotControlMessage("not json")).toBeNull();
  });
});


describe("withByotTarget", () => {
  it("returns to the same embedded chat without dropping profile or resume", () => {
    const original = new URLSearchParams("profile=work&resume=session-1");
    const external = withByotTarget(original, "terminal-1");
    expect(external.get("resume")).toBe("session-1");
    expect(external.get("profile")).toBe("work");
    expect(external.get("byot")).toBe("terminal-1");
    expect(withByotTarget(external, null).toString()).toBe(original.toString());
  });
});
