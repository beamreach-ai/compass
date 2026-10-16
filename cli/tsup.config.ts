import { defineConfig } from "tsup";

export default defineConfig([
  {
    entry: { bin: "src/bin.ts" },
    format: ["esm"],
    target: "node20",
    platform: "node",
    clean: true,
    sourcemap: true,
    banner: { js: "#!/usr/bin/env node" },
    shims: true,
  },
  {
    // what plugins import: @beamreach/compass/plugin
    entry: { plugin: "src/plugin.ts" },
    format: ["esm"],
    target: "node20",
    platform: "node",
    sourcemap: true,
    dts: true,
    shims: true,
  },
]);
