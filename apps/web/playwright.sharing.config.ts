import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e", testMatch: "project-sharing.spec.ts", workers: 1, retries: 0,
  reporter: "list", timeout: 60000,
  use: { baseURL: "http://127.0.0.1:3108", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: [
    { command: `${process.env.FORMA_TEST_PYTHON || "python"} -m tests.projects.project_sharing_browser_server`, cwd: "../..", url: "http://127.0.0.1:4178/test/health", reuseExistingServer: false, timeout: 30000 },
    { command: "npm run start -- --hostname 127.0.0.1 --port 3108", url: "http://127.0.0.1:3108", reuseExistingServer: false, timeout: 90000 },
  ],
});
