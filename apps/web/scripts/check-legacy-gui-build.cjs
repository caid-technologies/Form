// CI regression: reproduce the reported old-scope import failure against the
// base configuration, then compile those same JS/types/CSS with the repair.
// The test-only route and temporary config edits are always removed/restored.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const base = process.env.GUI_BASE_SHA;
if (!/^[a-f0-9]{40}$/.test(base || "")) throw new Error("GUI_BASE_SHA must be the base commit's full SHA.");
const fixture = path.join(root, "app/gui-scope-build-regression");
if (fs.existsSync(fixture)) throw new Error("Test route already exists; refusing to overwrite it.");
const reportDir = path.join(root, "gui-scope-results");
fs.mkdirSync(reportDir, { recursive: true });
const snapshots = new Map(["next.config.js", "tsconfig.json"].map((f) => [f, fs.readFileSync(path.join(root, f))]));
const originalFromGit = (filename) => {
  const result = spawnSync("git", ["show", `${base}:apps/web/${filename}`], { cwd: root, encoding: "utf8" });
  if (result.error || result.status !== 0) throw new Error(`Cannot read base ${filename}: ${result.stderr}`);
  return result.stdout;
};
// Resolve base files before mutating any working-tree files.
const baseFiles = new Map([...snapshots.keys()].map((f) => [f, originalFromGit(f)]));
function restoreConfig() { for (const [f, bytes] of snapshots) fs.writeFileSync(path.join(root, f), bytes); }
function build(label) {
  fs.rmSync(path.join(root, ".next"), { recursive: true, force: true });
  const result = spawnSync(process.execPath, [require.resolve("next/dist/bin/next"), "build"], {
    cwd: root, encoding: "utf8", timeout: 12 * 60 * 1000, maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env, NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1" },
  });
  const text = `${result.stdout || ""}\n${result.stderr || ""}`;
  fs.writeFileSync(path.join(reportDir, `${label}.log`), text);
  console.log(`=== ${label} ===\n${text}`);
  if (result.error) throw result.error;
  return { code: result.status, text };
}
const report = { base_sha: base, baseline_error_reproduced: false, repaired_build_passed: false };
try {
  fs.mkdirSync(fixture);
  fs.writeFileSync(path.join(fixture, "legacy.css"), '@import "@caid-technologies/forma-gui/styles.css";\n');
  fs.writeFileSync(path.join(fixture, "page.tsx"), `"use client";
import { FormaProjectBrowser, type FormaProjectSummary } from "@caid-technologies/forma-gui";
import "./legacy.css";
export default function GuiScopeRegression() {
  const projects: FormaProjectSummary[] = [];
  return <main><h1>GUI scope regression</h1><FormaProjectBrowser projects={projects} /></main>;
}
`);
  for (const [f, bytes] of baseFiles) fs.writeFileSync(path.join(root, f), bytes);
  const before = build("baseline");
  assert.notEqual(before.code, 0, "Base unexpectedly accepts the legacy scope; revisit the regression.");
  assert.match(before.text, /(?:Can't resolve|Cannot find module)[^\n]*@caid-technologies\/forma-gui/);
  report.baseline_error_reproduced = true;
  restoreConfig();
  const after = build("repaired");
  assert.equal(after.code, 0, "Repaired build must succeed with legacy JS, types and CSS.");
  const manifest = JSON.parse(fs.readFileSync(path.join(root, ".next/server/app-paths-manifest.json"), "utf8"));
  assert.ok(manifest["/gui-scope-build-regression/page"], "Regression page must be included, not skipped.");
  report.repaired_build_passed = true;
} finally {
  restoreConfig();
  fs.rmSync(fixture, { recursive: true, force: true });
  fs.writeFileSync(path.join(reportDir, "report.json"), JSON.stringify(report, null, 2));
}
console.log(JSON.stringify(report, null, 2));
