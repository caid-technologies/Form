import { expect, test, type Page } from "@playwright/test";
import type { RuntimeConfigContract } from "../lib/config";

const projectId = "746df932-f4ed-49a4-a36e-5a50a22aa918";
const chatId = "839aeb42-31d5-4b74-91f8-a93bc3a17db6";
const model = { provider: "openai", model: "gpt-5.5", label: "Test model", configured: true };
const runtime: RuntimeConfigContract = {
  contract_version: 1, authority: "backend", forma_dev_mode: false,
  generation: { ready: true, available: true, reason: null, selected_llm: model, llm_options: [model] },
  images: { enabled: false, configured: false, request_capable: false, provider: null, model: null, generate_by_default: false, reason: null },
  workflow: { default_id: "default", options: [{ id: "default", label: "Default", description: "Test" }] },
  provider_setup: { required: false, llm_required: false, image_required: false },
  deployment: { hosted_chat_enabled: true, authoring_mode_enabled: false, authoring_access: true, opencode_connector_id: "mini" },
  video: { generation: { configured: false }, self_correction: { configured: false } },
};
const project = {
  project_id: projectId, chat_id: chatId, can_chat: true, prompt: "Build a printable bracket",
  project_ir: {
    overview: { title: "Printable bracket", description: "A low-voltage enclosure bracket.", difficulty: "Beginner", estimated_cost: 5, category: "Mechanical" },
    part_definitions: [], components: [], connections: [], nets: [], assembly: [], constraints: [],
    validation: { critical: [], warning: [], info: [] }, assembly_metadata: { workflow: "default" },
  },
};
const pdf = { name: "reference.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n% mocked ingestion fixture\n%%EOF") };
const png = { name: "reference.png", mimeType: "image/png", buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+afoQAAAAASUVORK5CYII=", "base64") };

async function mockBackend(page: Page, baseURL: string, authoring = false) {
  const appOrigin = new URL(baseURL).origin;
  const chats = new Map<string, Record<string, unknown>>([[chatId, {
    chat_id: chatId, title: "Printable bracket", project_id: projectId,
    created_at: "2026-09-30T12:00:00Z", updated_at: "2026-09-30T12:00:00Z",
    messages: [
      { id: "user-existing", role: "user", content: "Build a printable bracket", status: "idle" },
      { id: "assistant-existing", role: "assistant", content: "The bracket is ready.", status: "success", projectId },
    ],
  }]]);
  const contextRequests: Array<{ projectId: string; body: { text: string; conversation_id: string; attachments: Array<{ kind: string; name: string; data_url: string }> } }> = [];
  const iterations: Array<{ instruction: string }> = [];
  const unexpected: string[] = [];
  const errors: string[] = [];
  let releaseFirst!: () => void;
  const firstReply = new Promise<void>((resolve) => { releaseFirst = resolve; });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
    const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
    const method = request.method();
    if (path === "/runtime/config") return route.fulfill({ json: { ...runtime, deployment: { ...runtime.deployment, authoring_mode_enabled: authoring, hosted_chat_enabled: !authoring } } });
    if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
    if (path === "/chats") return route.fulfill({ json: [...chats.values()] });
    if (path.startsWith("/chats/")) {
      const id = decodeURIComponent(path.slice("/chats/".length));
      if (method === "PUT") chats.set(id, { ...request.postDataJSON(), chat_id: id, updated_at: new Date().toISOString() });
      return route.fulfill({ status: chats.has(id) ? 200 : 404, json: chats.get(id) || { detail: "Chat not found" } });
    }
    if (path === `/projects/${projectId}`) return route.fulfill({ json: project });
    if (path === `/projects/${projectId}/history`) return route.fulfill({ json: { revisions: [] } });
    if (/^\/projects\/[^/]+\/context\/messages$/.test(path)) {
      const id = decodeURIComponent(path.split("/")[2]);
      contextRequests.push({ projectId: id, body: request.postDataJSON() });
      if (contextRequests.length === 1) await firstReply;
      return route.fulfill({ json: { assistant_message: `Context reply ${contextRequests.length}`, turn_kind: "context", design_brief: { project_id: id }, workflow: { project_id: id, state: "gathering_context" }, questions: [], suggestions: [] } });
    }
    if (path === `/projects/${projectId}/iterate`) {
      iterations.push(request.postDataJSON());
      return route.fulfill({ json: { ...project, message: "Project updated." } });
    }
    if (["/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
    if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
    if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
    if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
    if (path === "/" || method === "OPTIONS") return route.fulfill({ json: { status: "ok" } });
    unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 501, json: { detail: "Unmocked endpoint" } });
  });
  return { contextRequests, iterations, releaseFirst, unexpected, errors };
}

test.use({ serviceWorkers: "block" });
// CI's real-page suite uses Next dev; allow its first route compilation.
test.setTimeout(180_000);

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`attachments persist across first message, reply, and reload at ${viewport.width}px`, async ({ page, baseURL }) => {
    const backend = await mockBackend(page, baseURL!);
    await page.setViewportSize(viewport);
    try {
      await page.goto("/");
      const attach = page.getByRole("button", { name: "Attach image or PDF", exact: true });
      const input = page.getByLabel("Choose image or PDF");
      await expect(attach).toBeVisible();
      await input.setInputFiles(pdf);
      await expect(page.getByRole("button", { name: "Remove PDF" })).toBeVisible();
      await page.locator("textarea").fill("Use this bracket reference");
      await page.locator("textarea").press("Enter");
      await expect.poll(() => backend.contextRequests.length).toBe(1);
      await expect(attach).toBeVisible();
      expect(backend.contextRequests[0].body.attachments[0]).toMatchObject({ kind: "document", name: pdf.name });
      // Selecting the next turn's attachment while the first reply is pending
      // must survive the empty-to-active composer transition and reply render.
      await input.setInputFiles(png);
      backend.releaseFirst();
      await expect(page.getByText("Context reply 1", { exact: true })).toBeVisible();
      await expect(page.getByAltText("Attached prompt image")).toBeVisible();
      await expect(attach).toBeVisible();
      await page.locator("textarea").fill("Add the mounting holes from this image");
      await page.locator("textarea").press("Enter");
      await expect.poll(() => backend.contextRequests.length).toBe(2);
      expect(backend.contextRequests[1].projectId).toBe(backend.contextRequests[0].projectId);
      expect(backend.contextRequests[1].body.conversation_id).toBe(backend.contextRequests[0].body.conversation_id);
      expect(backend.contextRequests[1].body.attachments[0].kind).toBe("image");
      await expect(page.getByText("Context reply 2", { exact: true })).toBeVisible();
      await page.reload();
      await expect(attach).toBeVisible();
      await input.setInputFiles(pdf);
      await expect(page.getByRole("button", { name: "Remove PDF" })).toBeVisible();
      await page.screenshot({ path: `test-results/chat-attachments-${viewport.width}.png`, fullPage: true });
      expect(backend.unexpected).toEqual([]);
      expect(backend.errors).toEqual([]);
    } finally { backend.releaseFirst(); }
  });
}

test("saved project composer uploads later-turn references and preserves text iteration", async ({ page, baseURL }) => {
  const backend = await mockBackend(page, baseURL!);
  backend.releaseFirst();
  await page.goto(`/chat/${chatId}`);
  const attach = page.getByRole("button", { name: "Attach image or PDF", exact: true });
  const input = page.getByLabel("Choose image or PDF");
  await expect(attach).toBeVisible();
  await input.setInputFiles({ name: "wrong.txt", mimeType: "text/plain", buffer: Buffer.from("invalid") });
  await expect(page.locator("#project-input-notice")).toContainText("Attach an image or PDF");
  await input.setInputFiles({ ...pdf, buffer: Buffer.alloc(2 * 1024 * 1024 + 1) });
  await expect(page.locator("#project-input-notice")).toContainText("limited to 2 MB");
  expect(backend.contextRequests).toHaveLength(0);
  for (let turn = 1; turn <= 2; turn += 1) {
    await input.setInputFiles(pdf);
    await expect(page.getByRole("button", { name: "Remove PDF" })).toBeVisible();
    await page.locator("textarea").fill(`Use the bracket dimensions, turn ${turn}`);
    await page.locator("textarea").press("Enter");
    await expect.poll(() => backend.contextRequests.length).toBe(turn);
    expect(backend.contextRequests.at(-1)).toMatchObject({ projectId, body: { conversation_id: chatId, attachments: [{ kind: "document", name: pdf.name, data_url: `data:application/pdf;base64,${pdf.buffer.toString("base64")}` }] } });
    await expect(page.getByText(`Context reply ${turn}`, { exact: true })).toBeVisible();
  }
  await input.setInputFiles(pdf);
  await expect(page.getByRole("button", { name: "Remove PDF" })).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-attachments.png", fullPage: true });
  await page.getByRole("button", { name: "Remove PDF" }).click();
  await page.locator("textarea").fill("Make the bracket five millimeters wider");
  await page.locator("textarea").press("Enter");
  await expect.poll(() => backend.iterations.length).toBe(1);
  expect(backend.iterations[0].instruction).toBe("Make the bracket five millimeters wider");
  await page.reload();
  await expect(attach).toBeVisible();
  expect(backend.unexpected).toEqual([]);
  expect(backend.errors).toEqual([]);
});

test("FormaAgent retains its existing attachment restriction without dropping the draft", async ({ page, baseURL }) => {
  const backend = await mockBackend(page, baseURL!, true);
  await page.goto(`/chat/${chatId}`);
  await page.getByLabel("Choose image or PDF").setInputFiles(pdf);
  await page.locator("textarea").fill("Use these dimensions");
  await page.locator("textarea").press("Enter");
  await expect(page.getByRole("status").filter({ hasText: "attachments are not available" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove PDF" })).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue("Use these dimensions");
  expect(backend.contextRequests).toHaveLength(0);
  expect(backend.unexpected).toEqual([]);
  expect(backend.errors).toEqual([]);
});
