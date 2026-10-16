import { describe, expect, it } from "vitest";
import { TOOLS } from "../src/tools/index.js";
import { flagSpecs } from "../src/cli/zodflags.js";

describe("tool registry", () => {
  it("has unique tool names and CLI commands", () => {
    const names = TOOLS.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
    const commands = TOOLS.map((t) => t.cli.command);
    expect(new Set(commands).size).toBe(commands.length);
    for (const name of names) expect(name).toMatch(/^compass_[a-z_]+$/);
  });

  it("confirm-gated tools declare a confirm input", () => {
    for (const tool of TOOLS.filter((t) => t.confirm)) {
      expect(Object.keys(tool.input), tool.name).toContain("confirm");
    }
  });

  it("every input field maps to a CLI flag", () => {
    for (const tool of TOOLS) {
      const specs = flagSpecs(tool.input, ["confirm"]);
      const keys = Object.keys(tool.input).filter((k) => k !== "confirm");
      expect(specs.map((s) => s.key), tool.name).toEqual(keys);
      for (const pos of tool.cli.positionals || []) expect(keys, `${tool.name} positional ${pos}`).toContain(pos);
    }
  });

  it("read tools are marked readOnly unless they also write", () => {
    const readOnly = TOOLS.filter((t) => t.annotations?.readOnlyHint);
    for (const t of readOnly) expect(t.scope).toBe("read");
  });
});
