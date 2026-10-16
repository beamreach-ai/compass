import { describe, expect, it } from "vitest";
import { z } from "zod";
import { coerce, flagSpecs } from "../src/cli/zodflags.js";

describe("zod → flags", () => {
  it("derives kinds, requiredness and choices", () => {
    const specs = flagSpecs({
      action: z.enum(["a", "b"]),
      limit: z.number().int().optional(),
      wait: z.boolean().optional(),
      ids: z.array(z.string()).optional(),
      models: z.array(z.object({ slug: z.string() })).optional(),
      name: z.string().describe("A name"),
    });
    expect(specs.map((s) => [s.key, s.kind, s.required])).toEqual([
      ["action", "enum", true],
      ["limit", "number", false],
      ["wait", "boolean", false],
      ["ids", "string[]", false],
      ["models", "json", false],
      ["name", "string", true],
    ]);
    expect(specs[0].choices).toEqual(["a", "b"]);
    expect(specs[5].description).toBe("A name");
    expect(specs[3].flag).toBe("--ids <a,b,c>");
  });

  it("coerces flag values", () => {
    expect(coerce({ key: "n", kind: "number", flag: "", description: "", required: false }, "3")).toBe(3);
    expect(coerce({ key: "b", kind: "boolean", flag: "", description: "", required: false }, true)).toBe(true);
    expect(coerce({ key: "l", kind: "string[]", flag: "", description: "", required: false }, "a, b,,c")).toEqual(["a", "b", "c"]);
    expect(coerce({ key: "l", kind: "string[]", flag: "", description: "", required: false }, ["a,b", "c"])).toEqual(["a", "b", "c"]);
    expect(coerce({ key: "j", kind: "json", flag: "", description: "", required: false }, '[{"x":1}]')).toEqual([{ x: 1 }]);
  });
});
