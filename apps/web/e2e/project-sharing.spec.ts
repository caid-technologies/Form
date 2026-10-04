import { expect, test, type Page } from "@playwright/test";

const backend = "http://127.0.0.1:4178";
const owner = "sharing-browser-owner";

/** Real share/history HTTP handlers and SQLite; unrelated application services are stubbed. */
async function routeApi(page: Page, baseURL: string, isOwner: boolean) {
  const requests: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === new URL(baseURL).origin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
    const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
    requests.push(path);
    if (/^\/projects\/[^/]+(?:\/|$)/.test(path)) {
      const headers: Record<string, string> = {};
      if (isOwner) headers["X-Test-Owner"] = owner;
      const token = route.request().headers()["x-project-share"];
      if (token) headers["X-Project-Share"] = token;
      const response = await page.request.fetch(`${backend}${path}${url.search}`, { method: route.request().method(), headers });
      return route.fulfill({ response });
    }
    if (path === "/runtime/config") return route.fulfill({ json: {
      contract_version: 1, authority: "backend", forma_dev_mode: false,
      generation: { ready: true, available: true, reason: null, selected_llm: null, llm_options: [] },
      images: { enabled: false, configured: false, request_capable: false, provider: null, model: null, generate_by_default: false, reason: null },
      workflow: { default_id: "default", options: [{ id: "default", label: "Default" }] },
      provider_setup: { required: false, llm_required: false, image_required: false },
      deployment: { hosted_chat_enabled: false, authoring_mode_enabled: true, authoring_access: true, opencode_connector_id: null },
      video: { generation: { configured: false, reason: null }, self_correction: { configured: false, reason: null } },
    } });
    if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
    if (["/chats", "/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
    if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
    if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
    if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
    return route.fulfill({ json: { status: "ok" } });
  });
  return { requests, errors };
}

test.use({ serviceWorkers: "block" });

test("owner shares a saved version, anonymous recipient opens it, and one link can be revoked", async ({ page, browser, request, baseURL }) => {
  const seed = await (await request.post(`${backend}/test/reset`)).json();
  const tracking = await routeApi(page, baseURL!, true);
  // Exercise the visible URL fallback when clipboard permission is denied.
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", { value: { writeText: async () => { throw new Error("denied"); } } }));
  await page.goto(`/project/${seed.project_id}?revision=${seed.revisions[0]}`);
  await expect(page.getByTestId("revision-preview")).toHaveAttribute("data-revision", "1", { timeout: 15000 });
  await page.getByRole("button", { name: "Share project", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Share version 1", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Robot arm version 1", { exact: true })).toBeVisible();
  await expect(dialog.getByText("No links have been created.")).toBeVisible();

  async function createLink() {
    const response = page.waitForResponse((r) => r.request().method() === "POST" && r.url().includes(`/shared/${seed.revisions[0]}`));
    await dialog.getByRole("button", { name: "Create and copy link" }).click();
    const record = await (await response).json();
    await expect(dialog.getByRole("textbox", { name: "New share link" })).toHaveValue(new RegExp(`#share=${record.token}$`));
    await expect(dialog.getByRole("button", { name: "Create and copy link" })).toBeEnabled();
    return { ...record, url: await dialog.getByRole("textbox", { name: "New share link" }).inputValue() };
  }
  const first = await createLink();
  const second = await createLink();
  expect(first.token).not.toBe(second.token);
  await expect(dialog.getByText("Select and copy the link above.")).toBeVisible();
  // Escape and native dialog focus restoration remain usable on narrow screens.
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Share project", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Share project", exact: true }).click();
  await expect(dialog.getByRole("button", { name: `Revoke link ${first.id.slice(0, 8)}` })).toBeVisible();
  await page.screenshot({ path: `test-results/sharing-owner-${test.info().project.name}.png` });

  const anonymous = await browser.newContext({ viewport: page.viewportSize()! });
  try {
    const recipient = await anonymous.newPage();
    const recipientTracking = await routeApi(recipient, baseURL!, false);
    await recipient.goto(first.url);
    await expect(recipient.getByRole("heading", { name: "Robot arm version 1", exact: true }).first()).toBeVisible();
    await expect(recipient.getByText("Saved arm design 1.", { exact: true })).toBeVisible();
    await expect(recipient.getByTestId("chat-pane")).toHaveCount(0);
    await expect(recipient.getByRole("button", { name: "History", exact: true })).toHaveCount(0);
    expect(await recipient.locator("body").innerText()).not.toMatch(/Private instructions|private-chat|private-upload/);
    await request.post(`${backend}/test/save`);
    await recipient.reload();
    await expect(recipient.getByText("Saved arm design 1.", { exact: true })).toBeVisible();
    await dialog.getByRole("button", { name: `Revoke link ${first.id.slice(0, 8)}` }).click();
    await expect(dialog.getByText("Revoked", { exact: true })).toBeVisible();
    await recipient.reload();
    await expect(recipient.getByRole("main").getByRole("alert")).toContainText("This shared version is unavailable");
    await recipient.goto(second.url);
    await expect(recipient.getByText("Saved arm design 1.", { exact: true })).toBeVisible();
    await recipient.goto(first.url);
    await expect(recipient.getByRole("main").getByRole("alert")).toContainText("This shared version is unavailable");
    await expect(recipient.getByText("Saved arm design 1.", { exact: true })).toHaveCount(0);
    expect(recipientTracking.requests.every((path) => path.includes(`/shared/${seed.revisions[0]}`))).toBe(true);
    expect(recipientTracking.errors).toEqual([]);
  } finally { await anonymous.close(); }
  expect(tracking.errors).toEqual([]);
});
