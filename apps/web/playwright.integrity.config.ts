import { defineConfig } from "@playwright/test";
import config from "./playwright.config";

export default defineConfig({
  ...config,
  testMatch: ["opencode-chat.spec.ts", "chat-attachments.spec.ts", "gear-motion.spec.ts", "version-history.spec.ts"],
  workers: 1,
  use: { ...config.use, baseURL: "http://127.0.0.1:3107" },
  webServer: {
    command: "npm run dev -- --hostname 127.0.0.1 --port 3107",
    cwd: ".",
    url: "http://127.0.0.1:3107",
    reuseExistingServer: false,
    timeout: 180000,
    env: { FORMA_AUTH_MODE: "local" },
  },
});
