const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const { CANONICAL_GUI, LEGACY_GUI, formaGuiResolution } = require("../config/forma-gui-resolution.cjs");
const root = path.resolve(__dirname, "..");
const appRequire = createRequire(path.join(root, "package.json"));
const resolution = formaGuiResolution(root);

function readJson(filename) { return JSON.parse(fs.readFileSync(path.join(root, filename), "utf8")); }

test("only the canonical published package is an installed dependency", () => {
  const pkg = readJson("package.json");
  const lock = readJson("package-lock.json");
  assert.ok(pkg.dependencies[CANONICAL_GUI]);
  assert.equal(pkg.dependencies[LEGACY_GUI], undefined);
  assert.equal(lock.packages[""].dependencies[CANONICAL_GUI], pkg.dependencies[CANONICAL_GUI]);
  assert.ok(lock.packages[`node_modules/${CANONICAL_GUI}`]);
  assert.equal(lock.packages[`node_modules/${LEGACY_GUI}`], undefined);
  assert.throws(() => appRequire.resolve(LEGACY_GUI), { code: "MODULE_NOT_FOUND" });
});

test("legacy JS and CSS resolve to the canonical package's real exported files", () => {
  assert.deepEqual(Object.keys(resolution.webpackAliases).sort(), [`${LEGACY_GUI}$`, `${LEGACY_GUI}/styles.css$`].sort());
  assert.equal(resolution.webpackAliases[`${LEGACY_GUI}$`], appRequire.resolve(CANONICAL_GUI));
  const styles = resolution.webpackAliases[`${LEGACY_GUI}/styles.css$`];
  assert.equal(styles, appRequire.resolve(`${CANONICAL_GUI}/styles.css`));
  assert.ok(fs.statSync(styles).size > 0);
});

test("Turbopack aliases use the same public package exports", () => {
  assert.deepEqual(resolution.turbopackAliases, {
    [LEGACY_GUI]: CANONICAL_GUI,
    [`${LEGACY_GUI}/styles.css`]: `${CANONICAL_GUI}/styles.css`,
  });
});

test("TypeScript resolves legacy types to the same installed declarations", () => {
  const ts = appRequire("typescript");
  const raw = ts.readConfigFile(path.join(root, "tsconfig.json"), ts.sys.readFile);
  assert.equal(raw.error, undefined);
  const parsed = ts.parseJsonConfigFileContent(raw.config, ts.sys, root);
  const from = path.join(root, "app/forma-workspace.tsx");
  const resolve = (name) => ts.resolveModuleName(name, from, parsed.options, ts.sys).resolvedModule;
  assert.ok(resolve(LEGACY_GUI));
  assert.equal(resolve(LEGACY_GUI).resolvedFileName, resolve(CANONICAL_GUI).resolvedFileName);
});

test("the Next configuration consumes the mapping and preserves the CAD alias", () => {
  const config = fs.readFileSync(path.join(root, "next.config.js"), "utf8");
  assert.match(config, /\.\.\.guiResolution\.webpackAliases/);
  assert.match(config, /resolveAlias:\s*guiResolution\.turbopackAliases/);
  assert.match(config, /"opencad-viewport\$"/);
});

test("first-party app imports remain canonical instead of spreading the old name", () => {
  const matches = [];
  function visit(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, item.name);
      if (item.isDirectory()) visit(filename);
      else if (/\.(?:[cm]?[jt]sx?|css)$/.test(item.name) && fs.readFileSync(filename, "utf8").includes(LEGACY_GUI)) {
        matches.push(path.relative(root, filename));
      }
    }
  }
  for (const dir of ["app", "components", "lib"]) visit(path.join(root, dir));
  assert.deepEqual(matches, []);
});
