import { copyFileSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
mkdirSync(resolve(packageRoot, "dist"), { recursive: true });
copyFileSync(resolve(packageRoot, "src/styles.css"), resolve(packageRoot, "dist/styles.css"));

for (const file of readdirSync(resolve(packageRoot, "dist"), { recursive: true })) {
  if (typeof file !== "string" || !file.endsWith(".js")) continue;
  const path = resolve(packageRoot, "dist", file);
  const source = readFileSync(path, "utf8");
  const rewritten = source.replace(/((?:from\s+|import\s*\(\s*)["'])(\.[^"']+)(["'])/g, (match, prefix, specifier, suffix) => (
    /\.[cm]?[jt]sx?$/.test(specifier) ? match : `${prefix}${specifier}.js${suffix}`
  ));
  if (rewritten !== source) writeFileSync(path, rewritten);
}
