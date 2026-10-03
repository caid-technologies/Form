// Compatibility for consumers of the former scope. The published package and
// all first-party imports remain @isayahc/forma-gui; never fetch the old scope.
const { createRequire } = require("node:module");
const path = require("node:path");

const CANONICAL_GUI = "@isayahc/forma-gui";
const LEGACY_GUI = "@caid-technologies/forma-gui";

function formaGuiResolution(appRoot) {
  const appRequire = createRequire(path.resolve(appRoot, "package.json"));
  // Resolve through the installed package's exports, rather than hard-coding dist.
  const entry = appRequire.resolve(CANONICAL_GUI);
  const styles = appRequire.resolve(`${CANONICAL_GUI}/styles.css`);
  return {
    webpackAliases: {
      [`${LEGACY_GUI}$`]: entry,
      [`${LEGACY_GUI}/styles.css$`]: styles,
    },
    // Turbopack uses package names rather than webpack's exact-match `$` suffix.
    turbopackAliases: {
      [LEGACY_GUI]: CANONICAL_GUI,
      [`${LEGACY_GUI}/styles.css`]: `${CANONICAL_GUI}/styles.css`,
    },
  };
}

module.exports = { CANONICAL_GUI, LEGACY_GUI, formaGuiResolution };
