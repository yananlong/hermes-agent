import { describe, expect, it } from "vitest";
import { terminalAppearanceFromConfig, terminalFontSizeForWidth } from "./terminal-appearance";

describe("dashboard terminal appearance", () => {
  it("uses bundled defaults and accepts a family name or CSS stack", () => {
    const defaults = terminalAppearanceFromConfig({});
    expect(defaults.fontFamily).toMatch(/^'Source Code Pro'/);
    expect(defaults.fontSize).toBe(15);
    expect(terminalAppearanceFromConfig({ terminal: { font_family: "MesloLGS NF", font_size: 23 } }))
      .toEqual({ fontFamily: `'MesloLGS NF', ${defaults.fontFamily}`, fontSize: 23 });
    expect(terminalAppearanceFromConfig({ terminal: { font_family: "'Hack', monospace", font_size: 50 } }))
      .toEqual({ fontFamily: `'Hack', monospace, ${defaults.fontFamily}`, fontSize: 15 });
  });

  it("preserves the default narrow-screen tiers while scaling configured sizes", () => {
    expect(terminalFontSizeForWidth(320)).toBe(8);
    expect(terminalFontSizeForWidth(1200)).toBe(15);
    expect(terminalFontSizeForWidth(320, 23)).toBe(12);
    expect(terminalFontSizeForWidth(1200, 23)).toBe(23);
    expect(terminalFontSizeForWidth(200, 8)).toBe(7);
  });
});
