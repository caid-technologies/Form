import { expect, test, type Page } from "@playwright/test";

const projectId = "11111111-1111-4111-8111-111111111111";
const chatId = "22222222-2222-4222-8222-222222222222";
const revisionId = (version: number) => `33333333-3333-4333-8333-33333333333${version}`;
const titles = ["Robot arm base", "Robot arm with links", "Robot arm with gripper", "Robot arm with wrist"];
const summary = (version: number) => ({
  revision_id: revisionId(version), revision: version, parent_revision: version > 1 ? version - 1 : null,
  created_at: `2026-09-21T11:${String(version * 10).padStart(2, "0")}:00Z`, title: titles[version - 1],
  summary: ["Created base", "Added arm links", "Added gripper", "Added wrist"][version - 1],
});
const ir = (version: number) => ({
  overview: { title: titles[version - 1], description: `The robot arm as saved in version ${version}.`, difficulty: "Beginner", category: "Mechanical" },
  components: [], connections: [], nets: [], assembly: [], constraints: [],
  assembly_metadata: { project_id: projectId, chat_id: chatId, can_chat: true, revision: version, canonical_revision_id: revisionId(version), authoring_agent: "opencode" },
});
const runtime = {
  contract_version: 1, authority: "backend", forma_dev_mode: false,
  generation: { ready: true, available: true, reason: null, selected_llm: null, llm_options: [] },
  images: { enabled: false, configured: false, request_capable: false, provider: null, model: null, generate_by_default: false, reason: null },
  workflow: { default_id: "default", options: [{ id: "default", label: "Default" }] },
  provider_setup: { required: false, llm_required: false, image_required: false },
  deployment: { hosted_chat_enabled: false, authoring_mode_enabled: true, authoring_access: true, opencode_connector_id: "mini-pc-1" },
  video: { generation: { configured: false, reason: null }, self_correction: { configured: false, reason: null } },
};

async function setup(page: Page, baseURL: string) {
  const state = { latest: 3, missing: 0, holdFirst: false, releaseFirst: () => {}, snapshotReads: [] as number[], latestReads: 0, writes: [] as string[], errors: [] as string[] };
  const firstGate = new Promise<void>((resolve) => { state.releaseFirst = resolve; });
  const chat = { chat_id: chatId, title: "Robot arm design", created_at: "2026-09-21T11:00:00Z", updated_at: "2026-09-21T11:30:00Z", messages: [
    { id: "user-1", role: "user", content: "Add the arm links", status: "idle", timestamp: "2026-09-21T11:10:00Z", projectId },
    { id: "assistant-1", role: "assistant", content: "The arm links are saved in version 2.", status: "success", timestamp: "2026-09-21T11:20:00Z", projectId, revisionId: revisionId(2) },
  ] };
  page.on("pageerror", (error) => state.errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === new URL(baseURL).origin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
    const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
    if (!["GET", "OPTIONS"].includes(route.request().method())) state.writes.push(path);
    if (path === "/runtime/config") return route.fulfill({ json: runtime });
    if (path === "/chats") return route.fulfill({ json: [chat] });
    if (path === `/chats/${chatId}`) return route.fulfill({ json: chat });
    if (path === `/projects/${projectId}/history`) return route.fulfill({ json: {
      project_id: projectId, items: url.searchParams.has("before") ? [summary(1)] : [summary(state.latest), ...(state.latest === 4 ? [summary(3)] : []), summary(2)], latest_revision: state.latest,
      next_before: url.searchParams.has("before") ? null : 2,
    } });
    if (path.startsWith(`/projects/${projectId}/history/`)) {
      const version = Number(path.slice(-1));
      state.snapshotReads.push(version);
      if (version === 1 && state.holdFirst) await firstGate;
      if (version === state.missing) return route.fulfill({ status: 404, json: { detail: "Version unavailable" } });
      return route.fulfill({ json: { ...summary(version), project_id: projectId, project_ir: ir(version) } });
    }
    if (path === `/projects/${projectId}`) {
      state.latestReads += 1;
      return route.fulfill({ json: { project_id: projectId, chat_id: chatId, can_chat: true, project_ir: ir(state.latest) } });
    }
    if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
    if (["/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
    if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
    if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
    if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
    return route.fulfill({ json: { status: "ok" } });
  });
  return state;
}

test.use({ serviceWorkers: "block" });

test("browse exact versions from chat, export a snapshot, and return to the newest project", async ({ page, baseURL }) => {
  const state = await setup(page, baseURL!);
  await page.setViewportSize({ width: 1660, height: 1000 });
  await page.goto(`/chat/${chatId}`);
  const project = page.getByTestId("project-pane");
  await expect(project.getByRole("button", { name: "History", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "View this revision", exact: false }).click();
  const preview = page.getByTestId("revision-preview");
  await expect(preview).toHaveAttribute("data-revision", "2");
  await expect(preview.getByText("The robot arm as saved in version 2.", { exact: true })).toBeVisible();
  await expect(page.getByTestId("chat-pane")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`revision=${revisionId(2)}`));
  await page.screenshot({ path: "test-results/version-history-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "Load older versions" }).click();
  await page.getByRole("button", { name: /v1.*Created base/ }).click();
  await expect(preview).toHaveAttribute("data-revision", "1");
  await preview.getByRole("button", { name: "Exports", exact: true }).click();
  const downloadEvent = page.waitForEvent("download");
  await preview.getByRole("button", { name: "Project JSON", exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("project-v1.json");
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(JSON.parse(Buffer.concat(chunks).toString()).overview.title).toBe(titles[0]);
  state.latest = 4;
  await page.getByRole("button", { name: /Return to latest/ }).click();
  await expect(preview).toHaveCount(0);
  await expect(project.getByText("The robot arm as saved in version 4.", { exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/revision=/);
  await expect(project.getByText("v4 · Latest", { exact: true })).toBeVisible();
  expect(state.writes).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("snapshot races, unavailable versions, reloads, and mobile history preserve the selected version", async ({ page, baseURL }) => {
  const state = await setup(page, baseURL!);
  await page.setViewportSize({ width: 1660, height: 1000 });
  await page.goto(`/project/${projectId}?revision=${revisionId(2)}`);
  const preview = page.getByTestId("revision-preview");
  await expect(preview).toHaveAttribute("data-revision", "2");
  await page.getByRole("button", { name: "Load older versions" }).click();
  state.holdFirst = true;
  await page.getByRole("button", { name: /v1.*Created base/ }).click();
  await expect.poll(() => state.snapshotReads.includes(1)).toBe(true);
  await page.getByRole("button", { name: /v2.*Added arm links/ }).click();
  await expect(preview).toHaveAttribute("data-revision", "2");
  state.releaseFirst();
  state.holdFirst = false;
  await page.reload();
  await expect(preview).toHaveAttribute("data-revision", "2");
  await page.getByRole("button", { name: "Load older versions" }).click();
  state.missing = 1;
  await page.getByRole("button", { name: /v1.*Created base/ }).click();
  await expect(page.getByTestId("project-surface").getByRole("alert")).toContainText("unavailable");
  await expect(preview).toHaveCount(0);
  await expect(page.getByText("The robot arm as saved in version 3.", { exact: true })).toHaveCount(0);
  state.missing = 0;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(preview).toHaveAttribute("data-revision", "1");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: /v2.*Added arm links/ }).click();
  await expect(preview).toHaveAttribute("data-revision", "2");
  await expect(page.getByRole("complementary", { name: "Version history" })).toHaveCount(0);
  await page.getByRole("button", { name: "History", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "Version history" })).toBeVisible();
  await page.screenshot({ path: "test-results/version-history-mobile.png", fullPage: true });
  await page.getByRole("button", { name: "Close version history" }).click();
  await expect(preview).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.goto(`/chat/${chatId}?revision=${revisionId(2)}`);
  await expect(preview).toHaveAttribute("data-revision", "2");
  await expect(preview).toBeVisible();
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.getByTestId("chat-pane")).toBeVisible();
  await expect(preview).toHaveCount(0);
  await page.getByRole("button", { name: "Show project", exact: true }).click();
  await expect(preview).toHaveAttribute("data-revision", "2");
  expect(state.writes).toEqual([]);
  expect(state.errors).toEqual([]);
});
