import { buildProgram } from "./cli/program.js";
import { loadPlugins } from "./core/plugins.js";

const { plugins } = await loadPlugins(undefined, (line) => process.stderr.write(line + "\n"));
const program = buildProgram({ plugins });
program.parseAsync(process.argv).catch((error) => {
  process.stderr.write(`error: ${error?.message || String(error)}\n`);
  process.exit(1);
});
