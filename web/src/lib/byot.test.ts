import { describe, expect, it } from "vitest";

import { parseByotControlMessage } from "@/lib/byot";

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
