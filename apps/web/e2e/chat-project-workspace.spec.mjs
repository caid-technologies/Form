import { expect, test } from "@playwright/test";

const stats = (page) => page.evaluate(() => window.viewerStats);

test("desktop has one independent project pane and inline references, not inline viewers", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("chat-project-layout")).toHaveAttribute("data-layout", "split");
  await expect(page.getByTestId("model-canvas")).toHaveCount(1);
  await expect(page.getByTestId("message-scroller").locator("canvas")).toHaveCount(0);
  await expect(page.getByTestId("project-update-card")).toHaveCount(2);
  // Project detail uses /project; the slash inside this ID remains encoded.
  await expect(page.getByRole("link", { name: /Open linked project/ })).toHaveAttribute("href", "/project/archived%2Fcontroller");
  const chat = await page.getByTestId("chat-pane").boundingBox();
  const project = await page.getByTestId("project-pane").boundingBox();
  expect(chat && project && project.x > chat.x + chat.width).toBeTruthy();
  await page.screenshot({ path: "test-results/workspace-desktop.png", fullPage: true });
});

test("new updates do not remount or duplicate the active viewer", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("model-canvas")).toHaveCount(1);
  await page.getByRole("button", { name: "Orbit camera" }).click();
  const before = await stats(page);
  for (let i = 0; i < 12; i++) await page.getByRole("button", { name: "Complete update" }).click();
  await expect(page.getByTestId("project-update-card")).toHaveCount(14);
  await expect(page.getByTestId("camera-position")).toHaveText("1");
  expect(await stats(page)).toEqual(before);
});

test("close releases the viewer, cards reopen it, and repeated chat switching is bounded", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Close project", exact: true }).click();
  await expect(page.getByTestId("model-canvas")).toHaveCount(0);
  await expect.poll(async () => (await stats(page)).live).toBe(0);
  await page.getByRole("button", { name: /View current project/ }).click();
  await expect(page.getByTestId("model-canvas")).toHaveCount(1);
  for (let i = 0; i < 8; i++) {
    await page.getByRole("button", { name: "Bracket chat" }).click();
    await expect(page.getByTestId("model-canvas")).toHaveAttribute("aria-label", "Model for bracket");
    await page.getByRole("button", { name: "Controller chat" }).click();
    await expect(page.getByTestId("model-canvas")).toHaveAttribute("aria-label", "Model for controller");
    await expect.poll(async () => (await stats(page)).live).toBe(1);
  }
  await expect.poll(async () => (await stats(page)).live).toBe(1);
  expect((await stats(page)).peak).toBe(1);
});

test("full screen preserves the viewer, traps focus, and Escape restores the split", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Orbit camera" }).click();
  const before = await stats(page);
  await page.getByRole("button", { name: "View project full screen", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Project workspace" })).toBeVisible();
  await expect(page.getByTestId("chat-pane")).toBeHidden();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Orbit camera" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Exit project full screen", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Close project", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("chat-project-layout")).toHaveAttribute("data-layout", "split");
  await expect(page.getByTestId("camera-position")).toHaveText("1");
  expect(await stats(page)).toEqual(before);
  expect(await page.evaluate(() => document.querySelectorAll("[inert]").length)).toBe(0);
});

test("mobile mounts a viewer only in Project and keeps the composer draft", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByTestId("model-canvas")).toHaveCount(0);
  await page.getByRole("textbox", { name: "Describe a project change" }).fill("Keep this unsent draft");
  await page.getByRole("button", { name: /View current project/ }).click();
  await expect(page.getByTestId("model-canvas")).toHaveCount(1);
  await expect(page.getByTestId("chat-pane")).toBeHidden();
  await page.screenshot({ path: "test-results/workspace-mobile-project.png", fullPage: true });
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.getByTestId("model-canvas")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Describe a project change" })).toHaveValue("Keep this unsent draft");
  await page.screenshot({ path: "test-results/workspace-mobile-chat.png", fullPage: true });
});

test("only the selected project tab is mounted; resizing is keyboard accessible", async ({ page }) => {
  await page.goto("/");
  const separator = page.getByRole("separator", { name: "Resize chat and project" });
  await separator.focus();
  await page.keyboard.press("ArrowRight");
  await expect(separator).toHaveAttribute("aria-valuenow", "44");
  await page.keyboard.press("Home");
  await expect(separator).toHaveAttribute("aria-valuenow", "32");
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expect(page.getByTestId("model-canvas")).toHaveCount(0);
  await page.getByRole("button", { name: "CAD", exact: true }).click();
  await expect(page.getByTestId("model-canvas")).toHaveCount(1);
  expect((await stats(page)).peak).toBe(1);
});
