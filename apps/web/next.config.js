const path = require("path");
const { loadEnvConfig } = require("@next/env");
const { formaGuiResolution } = require("./config/forma-gui-resolution.cjs");

// Keep one shared environment surface at the repository root after moving the
// web application under apps/. Next still loads app-local overrides normally.
loadEnvConfig(path.resolve(__dirname, "../.."));

const guiResolution = formaGuiResolution(__dirname);

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  outputFileTracingRoot: path.resolve(__dirname),
  turbopack: {
    resolveAlias: guiResolution.turbopackAliases,
  },
  webpack(config) {
    config.resolve.alias = {
      ...config.resolve.alias,
      ...guiResolution.webpackAliases,
      "opencad-viewport$": path.resolve(__dirname, "node_modules/opencad-viewport/dist/index.js"),
    };
    return config;
  },
};

module.exports = nextConfig;

import("@opennextjs/cloudflare").then((module) =>
  module.initOpenNextCloudflareForDev(),
);
