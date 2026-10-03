import { expect, test, type Route } from "@playwright/test";
import type { RuntimeConfigContract } from "../lib/config";
import type { OpenCodeCommand, OpenCodeEvent, OpenCodeSession } from "../lib/opencode";

const projectId = "746df932-f4ed-49a4-a36e-5a50a22aa918";
const sessionId = "session-opencode-chat";
const commandIds = ["command-first-turn", "command-second-turn"];
const answers = ["Hello from OpenCode.", "Still here in the same OpenCode session."];
const legacyWelcome = {
  id: "assistant-welcome", role: "assistant", status: "idle", timestamp: "2026-10-03T15:06:00Z",
  content: "Tell me what you want to build. I can turn it into a project with parts, wiring, mechanical notes, validation, jobs, and optional product images.",
};
const publishedProject = {
  project_id: projectId,
  chat_id: "839aeb42-31d5-4b74-91f8-a93bc3a17db6",
  can_chat: true,
  prompt: "hi",
  project_ir: {
    overview: {
      title: "USB-powered status monitor",
      description: "A minimal low-voltage status monitor using an ESP32 development board.",
      difficulty: "Beginner",
      estimated_cost: 5,
      category: "Monitoring",
    },
    part_definitions: [{
      part_definition_id: "esp32",
      part_number: "ESP32-DevKitC",
      name: "ESP32 development board",
      category: "Microcontroller",
      unit_price: 5,
      rationale: "Provides USB power and an onboard controller for the status monitor.",
      pins: [],
    }],
    components: Array.from({ length: 9 }, (_, index) => ({
      ref_des: `U${index + 1}`, part_definition_id: "esp32", rationale: "Controller module",
    })),
    connections: [],
    nets: [],
    assembly: [],
    constraints: ["Power from 5V USB only."],
    validation: { critical: [], warning: [], info: [] },
    // Identity is supplied by the GET envelope, as consumed by withProjectResponseMetadata.
    assembly_metadata: { workflow: "default", source_prompt: "hi", authoring_agent: "opencode" },
  },
};

const runtimeConfig: RuntimeConfigContract = {
  contract_version: 1,
  authority: "backend",
  forma_dev_mode: false,
  generation: {
    ready: true,
    available: true,
    reason: null,
    selected_llm: null,
    llm_options: [],
  },
  images: {
    enabled: false,
    configured: false,
    request_capable: false,
    provider: null,
    model: null,
    generate_by_default: false,
    reason: null,
  },
  workflow: {
    default_id: "default",
    options: [{ id: "default", label: "Default", description: "Local FormaAgent authoring" }],
  },
  provider_setup: { required: false, llm_required: false, image_required: false },
  deployment: {
    hosted_chat_enabled: false,
    authoring_mode_enabled: true,
    authoring_access: true,
    opencode_connector_id: "mini-pc-1",
  },
  video: {
    generation: { configured: false, reason: null },
    self_correction: { configured: false, reason: null },
  },
};

function event(
  sequence: number,
  kind: OpenCodeEvent["kind"],
  overrides: Partial<OpenCodeEvent> = {},
): OpenCodeEvent {
  return {
    event_id: `event-${sequence}`,
    sequence,
    session_id: sessionId,
    project_id: projectId,
    kind,
    status: null,
    message: null,
    revision_id: null,
    error: null,
    created_at: "2026-09-12T12:00:00Z",
    ...overrides,
  };
}

// FORMA_AUTH_MODE=local must be supplied to the local web server, not mocked in the browser.
test.use({ serviceWorkers: "block" });

for (const scenario of ["concurrent chats", "stop during session creation"] as const) {
  test(`OpenCode isolates running state and cancellation: ${scenario}`, async ({ page, baseURL }) => {
    test.setTimeout(180_000);
    const appOrigin = new URL(baseURL!).origin;
    const sessions: OpenCodeSession[] = [];
    const commands: Array<{ sessionId: string; text: string }> = [];
    const cancellations: string[] = [];
    const completed = new Set<string>();
    const observedCompletions = new Set<string>();
    const chats = new Map<string, Record<string, unknown>>();
    const unexpected: string[] = [];
    const browserErrors: string[] = [];
    page.on("pageerror", (error) => browserErrors.push(error.message));
    let releaseSession!: () => void;
    const sessionGate = new Promise<void>((resolve) => { releaseSession = resolve; });

    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
      const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
      const method = request.method();
      if (path === "/runtime/config") return route.fulfill({ json: runtimeConfig });
      if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
      if (path === "/chats") return route.fulfill({ json: [...chats.values()] });
      if (path.startsWith("/chats/")) {
        const id = decodeURIComponent(path.slice("/chats/".length));
        if (method === "PUT") chats.set(id, { ...request.postDataJSON(), created_at: new Date().toISOString() });
        return route.fulfill({ status: chats.has(id) ? 200 : 404, json: chats.get(id) || { detail: "Chat not found" } });
      }
      if (["/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
      if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
      if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
      if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
      if (path === "/" || method === "OPTIONS") return route.fulfill({ json: { status: "ok" } });
      if (path === "/opencode/sessions" && method === "POST") {
        const session: OpenCodeSession = {
          session_id: `isolated-session-${sessions.length + 1}`,
          connector_id: "mini-pc-1",
          project_id: `isolated-project-${sessions.length + 1}`,
          owner_user_id: "local-user",
          status: "active",
        };
        sessions.push(session);
        if (scenario === "stop during session creation" && sessions.length === 1) await sessionGate;
        return route.fulfill({ json: session });
      }
      const session = sessions.find((item) => path.startsWith(`/opencode/sessions/${item.session_id}/`));
      if (session && path.endsWith("/commands") && method === "POST") {
        commands.push({ sessionId: session.session_id, text: request.postDataJSON().message });
        return route.fulfill({ json: {
          command_id: `${session.session_id}-command`, session_id: session.session_id,
          project_id: session.project_id, operation: "project_message", status: "queued",
        } satisfies OpenCodeCommand });
      }
      if (session && path.endsWith("/cancel") && method === "POST") {
        cancellations.push(session.session_id);
        return route.fulfill({ json: { ...session, status: "cancelled" } });
      }
      if (session && path.endsWith("/events")) {
        const cursor = Number(url.searchParams.get("cursor"));
        const events = completed.has(session.session_id) ? [
          event(2, "assistant_message", { session_id: session.session_id, project_id: session.project_id, message: `Answer for ${session.session_id}` }),
          event(3, "completed", { session_id: session.session_id, project_id: session.project_id, event_id: `${session.session_id}-command:terminal`, status: "succeeded" }),
        ].filter((item) => item.sequence > cursor) : cursor === 0 ? [
          event(1, "working", { session_id: session.session_id, project_id: session.project_id, status: "running" }),
        ] : [];
        return route.fulfill({ json: { events, next_cursor: events.at(-1)?.sequence ?? cursor } });
      }
      const project = sessions.find((item) => path === `/projects/${item.project_id}`);
      if (project) {
        observedCompletions.add(project.session_id);
        return route.fulfill({ status: 404, json: { detail: "Conversation did not create a project" } });
      }
      unexpected.push(`${method} ${path}`);
      return route.fulfill({ status: 501, json: { detail: "Unmocked endpoint" } });
    });

    try {
      await page.goto("/", { waitUntil: "domcontentloaded", timeout: 120_000 });
      const composer = page.getByPlaceholder("Describe the product, constraints, references, and outputs you need…", { exact: true });
      const stop = page.getByRole("button", { name: "Stop generation", exact: true });
      const newChat = page.getByRole("button", { name: "New chat", exact: true });
      await expect(composer).toBeVisible();
      await composer.fill("First independent request");
      await composer.press("Enter");
      await expect.poll(() => sessions.length).toBe(1);
      await expect(stop).toBeVisible();
      await expect(page).toHaveURL(/\/chat\/[^/?#]+$/);
      // Includes a session response held open: no agent reply has arrived yet.
      await expect(page.getByText(legacyWelcome.content, { exact: true })).toHaveCount(0);

      if (scenario === "stop during session creation") {
        await stop.click();
        await expect(stop).toHaveCount(0);
        // Start a replacement before the old create-session response arrives.
        await composer.fill("Replacement request");
        await composer.press("Enter");
        await expect.poll(() => commands.length).toBe(1);
        releaseSession();
        await expect.poll(() => cancellations).toEqual([sessions[0].session_id]);
        await expect(stop).toBeVisible();
        expect(commands.map((command) => command.sessionId)).toEqual([sessions[1].session_id]);
        await stop.click();
        await expect.poll(() => cancellations).toEqual(sessions.map((session) => session.session_id));
        await expect(stop).toHaveCount(0);
      } else {
        await expect.poll(() => commands.length).toBe(1);
        await newChat.click();
        await expect(stop).toHaveCount(0);
        await composer.fill("Second independent request");
        await expect(page.getByRole("button", { name: /Generate project/ })).toBeEnabled();
        await composer.press("Enter");
        await expect.poll(() => commands.length).toBe(2);
        await expect(stop).toBeVisible();
        // Enter cannot append another turn to a chat that is already running.
        await composer.fill("Duplicate request must not be submitted");
        await composer.press("Enter");
        await expect(composer).toHaveValue("Duplicate request must not be submitted");
        expect(commands).toHaveLength(2);

        await page.getByRole("button", { name: /^Open chat First independent request/ }).click();
        await expect(stop).toBeVisible();
        await stop.click();
        await expect.poll(() => cancellations).toEqual([sessions[0].session_id]);
        await expect(stop).toHaveCount(0);
        await page.getByRole("button", { name: /^Open chat Second independent request/ }).click();
        await expect(stop).toBeVisible();

        await newChat.click();
        await expect(stop).toHaveCount(0);
        await composer.fill("Third independent request");
        await composer.press("Enter");
        await expect.poll(() => commands.length).toBe(3);
        const thirdChatPath = new URL(page.url()).pathname;
        completed.add(sessions[1].session_id);
        await expect.poll(() => observedCompletions.has(sessions[1].session_id)).toBe(true);
        await expect(stop).toBeVisible();
        await expect(page).toHaveURL(new URL(thirdChatPath, baseURL!).href);
        await expect(page.getByRole("main").getByText(`Answer for ${sessions[1].session_id}`, { exact: false })).toHaveCount(0);
        await page.screenshot({ path: "test-results/opencode-isolated-running-chat.png", fullPage: true });
        await page.getByRole("button", { name: /^Open chat Second independent request/ }).click();
        await expect(stop).toHaveCount(0);
        await expect(page.getByRole("main").getByText(`Answer for ${sessions[1].session_id}`, { exact: false })).toBeVisible();
        await page.getByRole("button", { name: /^Open chat Third independent request/ }).click();
        await expect(stop).toBeVisible();
        await stop.click();
        await expect.poll(() => cancellations).toEqual([sessions[0].session_id, sessions[2].session_id]);
        await expect(stop).toHaveCount(0);
      }
      expect(unexpected).toEqual([]);
      expect(browserErrors).toEqual([]);
    } finally {
      releaseSession();
    }
  });
}

test("recover old OpenCode history on a clean browser without saving a fallback chat", async ({ page, baseURL }) => {
  test.setTimeout(180_000);
  const appOrigin = new URL(baseURL!).origin;
  const history = [
    { id: "first:user", role: "user", content: "Create a plain rounded-tooth gear.", status: "idle" },
    { id: "first:assistant", role: "assistant", content: "Saved your mechanical gear.", status: "success" },
    { id: "second:user", role: "user", content: "Generate an image of the same gear.", status: "idle" },
    { id: "second:assistant", role: "assistant", content: "Rendered the existing design.", status: "success" },
  ].map((message) => ({ ...message, projectId, timestamp: "2026-09-21T06:20:00Z" }));
  const mutations: string[] = [];
  const unexpected: string[] = [];
  let recoveries = 0;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
    const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
    if (!["GET", "OPTIONS"].includes(route.request().method())) mutations.push(path);
    if (path === "/runtime/config") return route.fulfill({ json: runtimeConfig });
    if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: {
      items: [{ ...publishedProject, title: "Rounded-Tooth Gear", created_at: "2026-09-21T06:20:00Z", visibility: "private" }], total: 1, has_more: false,
    } });
    if (path === `/projects/${projectId}`) return route.fulfill({ json: { ...publishedProject, prompt: "OpenCode project" } });
    if (path === `/chats/${publishedProject.chat_id}`) return route.fulfill({ status: 404, json: { detail: "Chat not found" } });
    if (path === `/opencode/projects/${projectId}/history`) {
      recoveries += 1;
      return route.fulfill({ json: { project_id: projectId, messages: [...history, legacyWelcome] } });
    }
    if (["/chats", "/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
    if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
    if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
    if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
    if (path === "/" || route.request().method() === "OPTIONS") return route.fulfill({ json: { status: "ok" } });
    unexpected.push(`${route.request().method()} ${path}`);
    return route.fulfill({ status: 501, json: { detail: "Unmocked endpoint" } });
  });
  await page.goto(`/chat/${publishedProject.chat_id}`, { waitUntil: "domcontentloaded", timeout: 120_000 });
  for (const message of history) await expect(page.getByRole("main").getByText(message.content, { exact: true })).toBeVisible();
  await expect(page.getByText(legacyWelcome.content, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("main").getByText("OpenCode project", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("main").getByText(/ is the active project for this chat\.$/)).toHaveCount(0);
  expect(recoveries).toBeGreaterThan(0);
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "domcontentloaded" });
  for (const message of history) await expect(page.getByRole("main").getByText(message.content, { exact: true })).toBeVisible();
  await expect(page.getByText(legacyWelcome.content, { exact: true })).toHaveCount(0);
  expect(mutations).toEqual([]);
  expect(unexpected).toEqual([]);
});

for (const accessResult of ["enabled", "disabled", "failed"] as const) {
  test(`Chat access loading handles ${accessResult} without a maintenance flash`, async ({ page, baseURL }) => {
    test.setTimeout(180_000);
    const appOrigin = new URL(baseURL!).origin;
    let releaseConfig!: () => void;
    const configGate = new Promise<void>((resolve) => { releaseConfig = resolve; });
    let configRequests = 0;
    const mutationRequests: string[] = [];

    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) {
        await route.continue();
        return;
      }
      const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
      if (route.request().method() === "POST") mutationRequests.push(path);
      if (path === "/runtime/config") {
        configRequests += 1;
        await configGate;
        if (accessResult === "failed" && configRequests === 1) {
          await route.fulfill({ status: 503, json: { detail: "Temporarily unavailable" } });
        } else {
          await route.fulfill({ json: {
            ...runtimeConfig,
            deployment: { ...runtimeConfig.deployment, authoring_access: accessResult !== "disabled" },
          } });
        }
        return;
      }
      if (["/projects", "/my/projects"].includes(path)) {
        await route.fulfill({ json: { items: [], total: 0, has_more: false } });
      } else if (["/chats", "/a2a/jobs", "/example-project-object-jobs"].includes(path)) {
        await route.fulfill({ json: [] });
      } else {
        await route.fulfill({ json: { status: "ok", is_admin: false, steps: [], models: [] } });
      }
    });

    try {
      await page.goto("/", { waitUntil: "domcontentloaded", timeout: 120_000 });
      const loading = page.getByRole("status").filter({ hasText: "Loading chat…" });
      const maintenance = page.getByRole("status", { name: "Hosted chat maintenance" });
      const composer = page.getByPlaceholder("Describe the product, constraints, references, and outputs you need…", { exact: true });
      await expect(loading).toBeVisible();
      await expect.poll(() => configRequests).toBe(1);
      await expect(maintenance).toHaveCount(0);
      await expect(composer).toHaveCount(0);
      releaseConfig();

      if (accessResult === "disabled") {
        // Fresh chats keep a writable composer; backend availability is checked
        // on submission instead of replacing it with a maintenance banner.
        await expect(composer).toBeVisible();
        await expect(maintenance).toHaveCount(0);
        await expect(page.getByRole("status", { name: "Forma Agent is authoring this workspace.", exact: true })).toHaveCount(0);
        await composer.fill("Build a simple mechanical mounting bracket.");
        await composer.press("Enter");
        await expect(page.getByText("Forma hosted chat is temporarily under maintenance.", { exact: true })).toBeVisible();
        expect(mutationRequests).toEqual([]);
      } else {
        if (accessResult === "failed") {
          await expect(page.getByRole("alert").filter({ hasText: "Chat could not be loaded" })).toBeVisible();
          await expect(maintenance).toHaveCount(0);
          await page.getByRole("button", { name: "Retry loading chat" }).click();
        }
        await expect(composer).toBeVisible();
        await expect(maintenance).toHaveCount(0);
      }
      await expect(loading).toHaveCount(0);
    } finally {
      releaseConfig();
    }
  });
}

for (const resultMode of ["unpublished", "published", "draft", "wired", "forbidden", "unavailable", "malformed", "null-response", "network-error", "missing-revision"] as const) test(`OpenCode chat handles ${resultMode} project output`, async ({ page, baseURL }) => {
  const projectPublished = ["published", "draft", "wired"].includes(resultMode);
  const resultLoadFails = !projectPublished && resultMode !== "unpublished";
  const readiness = resultMode === "draft" ? "draft" : resultMode === "wired" ? "complete" : "partial";
  const savedProject = structuredClone(publishedProject);
  if (resultMode === "draft") {
    savedProject.project_ir.components = [];
    savedProject.project_ir.part_definitions = [];
  }
  const wiredIR = {
    ...publishedProject.project_ir,
    part_definitions: [{
      ...publishedProject.project_ir.part_definitions[0],
      pins: [{ pin_id: "VBUS", name: "USB power", pin_type: "Power", voltage: 5 }],
    }],
    components: publishedProject.project_ir.components.slice(0, 2),
    nets: [{ net_id: "USB", name: "USB power", net_type: "Power", pins: [
      { ref_des: "U1", pin_id: "VBUS" }, { ref_des: "U2", pin_id: "VBUS" },
    ] }],
  };
  test.setTimeout(180_000);
  const appOrigin = new URL(baseURL!).origin;
  expect(["localhost", "127.0.0.1", "[::1]"]).toContain(new URL(appOrigin).hostname);

  const unexpectedRequests: string[] = [];
  const pageErrors: string[] = [];
  const configErrors: string[] = [];
  const sessionRequests: unknown[] = [];
  const commandRequests: { path: string; body: unknown }[] = [];
  const polls: { turn: number; cursor: number }[] = [];
  const completedTurns: number[] = [];
  const projectProbes: { turn: number; afterCompletion: boolean }[] = [];
  const projectResponseStatuses: number[] = [];
  const persistedChats = new Map<string, { chat_id: string; title: string; messages: { content: string; role: string; projectId?: string }[]; updated_at: string }>();
  let originalChatUrl = "";
  let releaseProgress!: () => void;
  let releaseCompletion!: () => void;
  const progressGate = new Promise<void>((resolve) => { releaseProgress = resolve; });
  const completionGate = new Promise<void>((resolve) => { releaseCompletion = resolve; });

  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && message.text().includes("Error fetching runtime config")) {
      configErrors.push(message.text());
    }
  });

  // Only local app assets/navigation may escape the mocks. Unknown backends fail closed.
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) {
      await route.continue();
      return;
    }
    unexpectedRequests.push(`${route.request().method()} ${url.href}`);
    await route.abort("blockedbyclient");
  });

  const mockBackend = async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
    const method = request.method();

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204 });
      return;
    }
    if (method === "GET" && path === "/runtime/config") {
      await route.fulfill({ json: runtimeConfig });
      return;
    }
    if (method === "GET" && path === "/") {
      await route.fulfill({ json: { status: "ok" } });
      return;
    }
    if (method === "GET" && ["/projects", "/my/projects"].includes(path)) {
      await route.fulfill({ json: {
        items: [], total: 0, has_more: false,
        limit: Number(url.searchParams.get("limit") || 6),
        offset: Number(url.searchParams.get("offset") || 0),
      } });
      return;
    }
    if (method === "GET" && path === "/chats") {
      await route.fulfill({ json: [...persistedChats.values()] });
      return;
    }
    if (method === "PUT" && path.startsWith("/chats/")) {
      const chatId = decodeURIComponent(path.slice("/chats/".length));
      const chat = { ...request.postDataJSON(), chat_id: chatId, updated_at: new Date().toISOString() };
      persistedChats.set(chatId, chat);
      await route.fulfill({ json: chat });
      return;
    }
    if (method === "GET" && path.startsWith("/chats/")) {
      const chat = persistedChats.get(decodeURIComponent(path.slice("/chats/".length)));
      await route.fulfill({ status: chat ? 200 : 404, json: chat || { detail: "Chat not found" } });
      return;
    }
    if (method === "GET" && ["/a2a/jobs", "/example-project-object-jobs"].includes(path)) {
      await route.fulfill({ json: [] });
      return;
    }
    if (method === "GET" && path === "/admin/session") {
      await route.fulfill({ json: { is_admin: false } });
      return;
    }
    if (method === "GET" && path === "/pipeline/steps") {
      await route.fulfill({ json: { steps: [] } });
      return;
    }
    if (method === "GET" && path === "/video/models") {
      await route.fulfill({ json: { models: [], generation_configured: false } });
      return;
    }
    if (method === "POST" && path === "/opencode/sessions") {
      sessionRequests.push(request.postDataJSON());
      const session: OpenCodeSession = {
        session_id: sessionId,
        connector_id: "mini-pc-1",
        project_id: projectId,
        owner_user_id: "local-user",
        status: "active",
      };
      await route.fulfill({ json: session });
      return;
    }
    if (method === "POST" && path === `/opencode/sessions/${sessionId}/commands`) {
      commandRequests.push({ path, body: request.postDataJSON() });
      const command: OpenCodeCommand = {
        command_id: commandIds[commandRequests.length - 1],
        session_id: sessionId,
        project_id: projectId,
        operation: "project_message",
        status: "queued",
      };
      await route.fulfill({ json: command });
      return;
    }
    if (method === "GET" && path === `/opencode/sessions/${sessionId}/events`) {
      const cursor = Number(url.searchParams.get("cursor"));
      const turn = commandRequests.length;
      polls.push({ turn, cursor });
      let events: OpenCodeEvent[] = [];
      if (turn === 1 && cursor === 0) {
        events = [event(1, "connector_unavailable", {
          event_id: `connector_unavailable_${sessionId}`,
          message: "Waiting for the local OpenCode connector.",
        })];
      } else if (turn === 1 && cursor === 1) {
        await progressGate;
        events = [event(2, "queued", { status: "queued" }), event(3, "working", { status: "running" })];
      } else if ((turn === 1 && cursor === 3) || (turn === 2 && cursor === 5)) {
        if (turn === 1) await completionGate;
        const sequence = turn === 1 ? 4 : 6;
        // The canonical terminal event shares the page with the answer but has no message.
        events = [
          event(sequence, "assistant_message", { message: answers[turn - 1] }),
          event(sequence + 1, "completed", {
            event_id: `${commandIds[turn - 1]}:terminal`,
            status: "succeeded",
            revision_id: resultMode === "missing-revision" ? "saved-revision" : null,
          }),
        ];
        completedTurns.push(turn);
      }
      await route.fulfill({ json: { events, next_cursor: events.at(-1)?.sequence ?? cursor } });
      return;
    }
    if (method === "GET" && path === `/projects/${projectId}`) {
      const turn = commandRequests.length;
      projectProbes.push({ turn, afterCompletion: completedTurns.includes(turn) });
      if (resultMode === "network-error") {
        await route.abort("failed");
        return;
      }
      const status = resultMode === "forbidden" ? 403
        : resultMode === "unavailable" ? 503
        : resultMode === "malformed" || resultMode === "null-response" ? 200
        : !projectPublished ? 404 : projectProbes.length === 1 ? 503 : 200;
      projectResponseStatuses.push(status);
      await route.fulfill({
        status,
        json: resultMode === "null-response" ? null : resultMode === "malformed" ? { project_id: projectId } : status === 200 ? { ...savedProject, project_ir: resultMode === "wired" ? wiredIR : savedProject.project_ir, project_readiness: readiness } : {
          detail: status === 404 ? "Project not found" : "Project store temporarily unavailable",
        },
      });
      return;
    }

    unexpectedRequests.push(`${method} ${url.href}`);
    await route.fulfill({ status: 501, json: { detail: `Unmocked endpoint: ${method} ${path}` } });
  };

  await page.route("**/api/**", mockBackend);
  await page.route(/^https?:\/\/(?:localhost|127\.0\.0\.1):8000(?:\/|$)/, mockBackend);
  await page.clock.install();

  try {
    try {
      await page.goto("/", { waitUntil: "domcontentloaded", timeout: 120_000 });
    } catch (error) {
      // Match the existing suite's workaround for a first Next dev compilation.
      if (!String(error).includes("ERR_ABORTED")) throw error;
      await page.goto("/", { waitUntil: "domcontentloaded", timeout: 120_000 });
    }

    const composer = page.getByPlaceholder("Describe the product, constraints, references, and outputs you need\u2026", { exact: true });
    const followUpComposer = projectPublished ? page.getByRole("textbox", { name: /Describe a change to/ }) : composer;
    const stop = page.getByRole("button", { name: "Stop generation", exact: true });
    const missingProject = page.getByText(/no longer available in (?:the )?project database/i);
    const projectLinks = page.locator(`a[href*="${projectId}"]`);
    const projectOutput = page.getByTestId("project-pane");
    const firstAnswer = page.getByRole("main").getByText(answers[0], { exact: false });
    const secondAnswer = page.getByRole("main").getByText(answers[1], { exact: false });

    await expect(page.getByRole("status", { name: "Forma Agent is authoring this workspace.", exact: true })).toHaveCount(0);
    await expect(composer).toBeVisible();

    await expect(page.getByRole("textbox", { name: "Agent model", exact: true })).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: "Switch agent model" })).toHaveCount(0);
    // Old picker preferences must not override the configured runtime model.
    await page.evaluate(() => localStorage.setItem("forma.agent.model", "google/gemini-2.5-flash"));

    await test.step("keep polling after connector_unavailable without loading the reserved project", async () => {
      await composer.fill("hi");
      await composer.press("Enter");
      await expect.poll(() => polls).toContainEqual({ turn: 1, cursor: 1 });
      await expect(page).toHaveURL(/\/chat\/[^/?#]+$/);
      originalChatUrl = page.url();
      expect(new URL(originalChatUrl).pathname.split("/").at(-1)).not.toBe(publishedProject.chat_id);
      await expect(stop).toBeVisible();
      expect(sessionRequests).toEqual([{ connector_id: "mini-pc-1" }]);
      expect(projectProbes).toEqual([]);
      await expect(missingProject).toHaveCount(0);
      await expect(projectLinks).toHaveCount(0);

      releaseProgress();
      await expect.poll(() => polls).toContainEqual({ turn: 1, cursor: 3 });
      await expect(stop).toBeVisible();
      expect(projectProbes).toEqual([]);
      await expect(missingProject).toHaveCount(0);
      await expect(projectLinks).toHaveCount(0);
    });

    await test.step(projectPublished
      ? "preserve the answer and original chat URL after canonical completion and a 503 retry"
      : "preserve the answer after canonical completion and exactly one 404 probe", async () => {
      releaseCompletion();
      if (resultLoadFails) {
        await expect.poll(() => projectProbes.length).toBeGreaterThan(0);
        await page.clock.runFor(6_000);
        await expect(stop).toHaveCount(0);
        await expect(page.getByRole("main").getByText(/OpenCode.*(?:project could not be loaded|no usable Hardware Intermediate Representation|project is not available)/).first()).toBeVisible();
        await expect(page.getByRole("main").getByText(/Hello from OpenCode/).first()).toBeVisible();
        await expect(projectOutput).toHaveCount(0);
        await expect(page).toHaveURL(originalChatUrl);
        expect(projectProbes).toHaveLength(resultMode === "unavailable" || resultMode === "network-error" ? 3 : 1);
        await page.clock.runFor(10_000);
        expect(polls).toHaveLength(3);
        return;
      }
      await expect(firstAnswer).toBeVisible();
      if (projectPublished) {
        await expect.poll(() => projectResponseStatuses[0]).toBe(503);
      } else {
        await expect(stop).toHaveCount(0);
        await expect.poll(() => projectProbes).toEqual([{ turn: 1, afterCompletion: true }]);
      }

      // Advance beyond the poll and hydration retry delays without a wall-clock sleep.
      await page.clock.runFor(6_000);
      await expect(stop).toHaveCount(0);
      await expect(firstAnswer).toBeVisible();
      await expect(firstAnswer).toHaveCount(1);
      await expect(missingProject).toHaveCount(0);
      await expect(page).toHaveURL(originalChatUrl);
      if (projectPublished) {
        await expect.poll(() => projectResponseStatuses.slice(0, 2)).toEqual([503, 200]);
        await expect(projectOutput).toBeVisible();
        if (resultMode === "draft") await expect(page.getByText(/Draft saved only/).first()).toBeVisible();
        if (resultMode === "published") await expect(page.getByText(/An incomplete design was saved/).first()).toBeVisible();
        if (resultMode === "wired") await expect(page.getByText(/Draft saved only|An incomplete design was saved/)).toHaveCount(0);
        await expect(projectOutput.getByRole("heading", { name: publishedProject.project_ir.overview.title, exact: true })).toBeVisible();
        await expect(projectOutput.getByText(publishedProject.project_ir.overview.description, { exact: true })).toBeVisible();
        // Successful publication may also trigger route/inline hydration GETs.
        expect(projectProbes.every((probe) => probe.turn === 1 && probe.afterCompletion)).toBe(true);
        expect(projectResponseStatuses.slice(1).every((status) => status === 200)).toBe(true);
      } else {
        await expect(projectLinks).toHaveCount(0);
        await expect(projectOutput).toHaveCount(0);
        expect(projectProbes).toEqual([{ turn: 1, afterCompletion: true }]);
      }
      expect(polls).toEqual([{ turn: 1, cursor: 0 }, { turn: 1, cursor: 1 }, { turn: 1, cursor: 3 }]);
    });

    if (resultLoadFails) {
      expect(unexpectedRequests).toEqual([]);
      expect(pageErrors).toEqual([]);
      return;
    }

    await test.step("send a second turn in the same session and original UI chat", async () => {
      await expect(page.getByRole("textbox", { name: "Agent model", exact: true })).toHaveCount(0);
      await expect(page.getByRole("combobox", { name: "Switch agent model" })).toHaveCount(0);
      await followUpComposer.fill("Are you still there?");
      await followUpComposer.press("Enter");
      await expect(secondAnswer).toBeVisible();
      await expect(stop).toHaveCount(0);
      if (projectPublished) {
        await expect.poll(() => projectProbes).toContainEqual({ turn: 2, afterCompletion: true });
      } else {
        await expect.poll(() => projectProbes).toEqual([
          { turn: 1, afterCompletion: true }, { turn: 2, afterCompletion: true },
        ]);
      }
      await page.clock.runFor(6_000);
      await expect(firstAnswer).toBeVisible();
      await expect(firstAnswer).toHaveCount(1);
      await expect(secondAnswer).toBeVisible();
      await expect(secondAnswer).toHaveCount(1);
      await expect(missingProject).toHaveCount(0);
      await expect(page).toHaveURL(originalChatUrl);
      if (projectPublished) {
        await expect(projectOutput).toBeVisible();
        await expect(projectOutput.getByRole("heading", { name: publishedProject.project_ir.overview.title, exact: true })).toBeVisible();
        expect(projectProbes.every((probe) => probe.afterCompletion)).toBe(true);
        expect(projectResponseStatuses.slice(1).every((status) => status === 200)).toBe(true);
      } else {
        await expect(projectLinks).toHaveCount(0);
        expect(projectProbes).toHaveLength(2);
      }
      expect(sessionRequests).toEqual([{ connector_id: "mini-pc-1" }]);
      expect(commandRequests).toEqual(["hi", "Are you still there?"].map((message) => ({
        path: `/opencode/sessions/${sessionId}/commands`,
        body: { message, idempotency_key: expect.stringMatching(/^web-.+/) },
      })));
      expect(polls).toEqual([
        { turn: 1, cursor: 0 }, { turn: 1, cursor: 1 }, { turn: 1, cursor: 3 }, { turn: 2, cursor: 5 },
      ]);
    });

    if (projectPublished) await test.step("the real OpenCode page shares one pane and preserves mobile drafts", async () => {
      const layout = page.getByTestId("chat-project-layout");
      await expect(layout).toHaveAttribute("data-layout", "split");
      await expect(projectOutput).toHaveCount(1);
      await expect(page.getByTestId("chat-pane").getByTestId("project-pane")).toHaveCount(0);
      await expect(page.getByTestId("chat-pane").locator("canvas")).toHaveCount(0);
      const currentCard = page.getByRole("button", { name: /View current project/ }).last();
      await expect(currentCard).toBeVisible();
      await page.getByRole("button", { name: "Close project", exact: true }).click();
      await expect(projectOutput).toHaveCount(0);
      await currentCard.click();
      await expect(projectOutput).toHaveCount(1);
      await page.getByRole("button", { name: "View project full screen", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "Project workspace", exact: true })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(layout).toHaveAttribute("data-layout", "split");
      await expect(projectOutput).toHaveCount(1);
      await page.screenshot({ path: `test-results/opencode-${resultMode}-workspace-desktop.png`, fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.getByRole("button", { name: "Chat", exact: true }).click();
      await expect(projectOutput).toHaveCount(0);
      await followUpComposer.fill("Preserve this mobile draft");
      await page.getByRole("button", { name: "Show project", exact: true }).click();
      await expect(projectOutput).toHaveCount(1);
      await expect(page.getByTestId("chat-pane")).toBeHidden();
      await page.getByRole("button", { name: "Chat", exact: true }).click();
      await expect(projectOutput).toHaveCount(0);
      await expect(followUpComposer).toHaveValue("Preserve this mobile draft");
      await page.screenshot({ path: `test-results/opencode-${resultMode}-workspace-mobile.png`, fullPage: true });
      await followUpComposer.fill("");
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.getByRole("button", { name: "Show project", exact: true }).click();
      await expect(layout).toHaveAttribute("data-layout", "split");
    });

    if (resultMode === "published") await test.step("restore both turns from the server after clearing browser storage", async () => {
      await page.clock.runFor(1_000);
      const chatId = new URL(originalChatUrl).pathname.split("/").at(-1)!;
      await expect.poll(() => persistedChats.get(chatId)?.messages.filter((message) => message.role === "user").map((message) => message.content)).toEqual(["hi", "Are you still there?"]);
      expect(persistedChats.get(chatId)?.messages.map((message) => message.content)).toEqual(expect.arrayContaining(answers.map((answer) => expect.stringContaining(answer))));
      await page.evaluate(() => localStorage.clear());
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(firstAnswer).toBeVisible();
      await expect(secondAnswer).toBeVisible();
      await expect(projectOutput).toBeVisible();
      await expect(page).toHaveURL(originalChatUrl);
      expect(commandRequests).toHaveLength(2);
      expect(sessionRequests).toHaveLength(1);
      await expect(page.getByRole("main").getByText("OpenCode project", { exact: true })).toHaveCount(0);
    });

    await test.step("New chat resets the rendered conversation with legacy hosted chat disabled", async () => {
      const previousUrl = page.url();
      const newChat = page.getByRole("button", { name: "New chat", exact: true }).filter({ visible: true });
      await expect(newChat).toBeEnabled();
      await followUpComposer.fill("Unsent draft");
      await newChat.click();
      await expect(page).not.toHaveURL(previousUrl);
      await page.clock.runFor(6_000);
      await expect(composer).toHaveValue("");
      await expect(firstAnswer).toHaveCount(0);
      await expect(secondAnswer).toHaveCount(0);
      await expect(newChat).toBeDisabled();
      await expect(page.getByRole("status", { name: "Hosted chat maintenance", exact: true })).toHaveCount(0);
      await expect(missingProject).toHaveCount(0);
      await expect(projectLinks).toHaveCount(0);
      await expect(projectOutput).toHaveCount(0);
      await expect(page.getByRole("textbox", { name: "Agent model", exact: true })).toHaveCount(0);
      await expect(page.getByRole("combobox", { name: "Switch agent model" })).toHaveCount(0);
    });

    expect(unexpectedRequests, "Every backend request must be mocked; no external requests may escape").toEqual([]);
    expect(configErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  } finally {
    releaseProgress();
    releaseCompletion();
  }
});

for (const outcome of ["retry", "cancel", "reconnect"] as const) {
  test(`OpenCode outage: ${outcome}`, async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    const appOrigin = new URL(baseURL!).origin;
    const sessions: OpenCodeSession[] = [];
    const commands: Array<{ session: string; message: string; key: string }> = [];
    const cancellations: string[] = [];
    const chats = new Map<string, Record<string, unknown>>();
    const unexpected: string[] = [];
    const errors: string[] = [];
    let resolveOutage = false;
    const polls: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
      const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
      const method = request.method();
      if (path === "/runtime/config") return route.fulfill({ json: runtimeConfig });
      if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
      if (path === "/chats") return route.fulfill({ json: [...chats.values()] });
      if (path.startsWith("/chats/")) {
        const id = decodeURIComponent(path.slice("/chats/".length));
        if (method === "PUT") chats.set(id, { ...request.postDataJSON(), created_at: new Date().toISOString() });
        return route.fulfill({ status: chats.has(id) ? 200 : 404, json: chats.get(id) || { detail: "Chat not found" } });
      }
      if (["/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
      if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
      if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
      if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
      if (path === "/" || method === "OPTIONS") return route.fulfill({ json: { status: "ok" } });
      if (path === "/opencode/sessions" && method === "POST") {
        const session: OpenCodeSession = { session_id: `outage-${sessions.length + 1}`, connector_id: "mini-pc-1", project_id: projectId, owner_user_id: "local-user", status: "active" };
        sessions.push(session);
        return route.fulfill({ json: session });
      }
      const session = sessions.find((item) => path.startsWith(`/opencode/sessions/${item.session_id}/`));
      if (session && path.endsWith("/commands") && method === "POST") {
        const body = request.postDataJSON();
        commands.push({ session: session.session_id, message: body.message, key: body.idempotency_key });
        return route.fulfill({ json: { command_id: `${session.session_id}-command`, session_id: session.session_id, project_id: projectId, operation: "project_message", status: "queued" } });
      }
      if (session && path.endsWith("/cancel")) {
        cancellations.push(session.session_id);
        return route.fulfill({ json: { ...session, status: "cancelled" } });
      }
      if (session && path.endsWith("/events")) {
        polls.push(session.session_id);
        const cursor = Number(url.searchParams.get("cursor"));
        const timedOut = outcome === "retry" && session.session_id === "outage-1";
        const events = [event(1, "connector_unavailable", { session_id: session.session_id })];
        if (resolveOutage) {
          if (!timedOut) events.push(event(2, "assistant_message", { session_id: session.session_id, message: "Connection restored; your request is complete." }));
          events.push(event(3, timedOut ? "failed" : "completed", {
            session_id: session.session_id, event_id: `${session.session_id}-command:terminal`,
            status: timedOut ? "failed" : "succeeded",
            error: timedOut ? { code: "connector_timeout", message: "Forma Agent did not reconnect within five minutes. Retry this request when it is available.", correlation_id: "outage-test" } : null,
          }));
        }
        const filtered = events.filter((item) => item.sequence > cursor);
        return route.fulfill({ json: { events: filtered, next_cursor: filtered.at(-1)?.sequence ?? cursor } });
      }
      if (path === `/projects/${projectId}`) return route.fulfill({ status: 404, json: { detail: "No saved project" } });
      unexpected.push(`${method} ${path}`);
      return route.fulfill({ status: 501, json: { detail: "Unmocked endpoint" } });
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    const composer = page.getByPlaceholder("Describe the product, constraints, references, and outputs you need…", { exact: true });
    const stop = page.getByRole("button", { name: "Stop generation", exact: true });
    await composer.fill("Build a printable hinge");
    await composer.press("Enter");
    await expect(page.getByText(/Waiting for Forma Agent to reconnect/)).toBeVisible();
    await expect(stop).toBeVisible();
    await expect(page.getByText(legacyWelcome.content, { exact: true })).toHaveCount(0);
    if (outcome === "cancel") {
      await stop.click();
      await expect.poll(() => cancellations).toEqual(["outage-1"]);
      await expect(stop).toHaveCount(0);
      await expect(page.getByText(/Code: connector_timeout/)).toHaveCount(0);
    } else {
      resolveOutage = true;
      if (outcome === "retry") {
        await expect(page.getByText(/Code: connector_timeout/)).toBeVisible();
        await expect(stop).toHaveCount(0);
        await expect(page.getByText(legacyWelcome.content, { exact: true })).toHaveCount(0);
        const retry = page.getByRole("button", { name: "Try failed build again", exact: true });
        await expect(retry).toBeVisible();
        await page.screenshot({ path: "test-results/opencode-timeout-retry.png", fullPage: true });
        const oldPollCount = polls.filter((session) => session === "outage-1").length;
        await retry.click();
        await expect.poll(() => sessions.length).toBe(2);
        await expect(page.getByText("Connection restored; your request is complete.", { exact: false })).toBeVisible();
        expect(commands.map((command) => command.message)).toEqual(["Build a printable hinge", "Build a printable hinge"]);
        expect(commands[0].key).not.toBe(commands[1].key);
        expect(polls.filter((session) => session === "outage-1")).toHaveLength(oldPollCount);
        await expect(retry).toHaveCount(0);
      } else {
        await expect(page.getByText("Connection restored; your request is complete.", { exact: false })).toBeVisible();
        expect(sessions).toHaveLength(1);
      }
      await expect(stop).toHaveCount(0);
    }
    expect(unexpected).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const mode of ["http", "malformed", "hung", "transient", "cancel", "reload"] as const) {
  test(`OpenCode polling recovery: ${mode}`, async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    const appOrigin = new URL(baseURL!).origin;
    const sessions: OpenCodeSession[] = [];
    const commands: Array<{ session: string; message: string; key: string }> = [];
    const cancellations: string[] = [];
    const chats = new Map<string, Record<string, unknown>>();
    const unexpected: string[] = [];
    const errors: string[] = [];
    const polls: string[] = [];
    const pending: Array<() => Promise<void>> = [];
    let recovered = false;
    let failures = 0;
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
      const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
      const method = request.method();
      if (path === "/runtime/config") return route.fulfill({ json: runtimeConfig });
      if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
      if (path === "/chats") return route.fulfill({ json: [...chats.values()] });
      if (path.startsWith("/chats/")) {
        const id = decodeURIComponent(path.slice("/chats/".length));
        if (method === "PUT") chats.set(id, { ...request.postDataJSON(), created_at: new Date().toISOString() });
        return route.fulfill({ status: chats.has(id) ? 200 : 404, json: chats.get(id) || { detail: "Chat not found" } });
      }
      if (["/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
      if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
      if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
      if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
      if (path === "/" || method === "OPTIONS") return route.fulfill({ json: { status: "ok" } });
      if (path === "/opencode/sessions" && method === "POST") {
        const session: OpenCodeSession = { session_id: `outage-${sessions.length + 1}`, connector_id: "mini-pc-1", project_id: projectId, owner_user_id: "local-user", status: "active" };
        sessions.push(session);
        return route.fulfill({ json: session });
      }
      const session = sessions.find((item) => path.startsWith(`/opencode/sessions/${item.session_id}/`));
      if (session && path.endsWith("/commands") && method === "POST") {
        const body = request.postDataJSON();
        commands.push({ session: session.session_id, message: body.message, key: body.idempotency_key });
        return route.fulfill({ json: { command_id: `${session.session_id}-command`, session_id: session.session_id, project_id: projectId, operation: "project_message", status: "queued" } });
      }
      if (session && path.endsWith("/cancel")) {
        cancellations.push(session.session_id);
        return route.fulfill({ json: { ...session, status: "cancelled" } });
      }
      if (session && path.endsWith("/events")) {
        polls.push(session.session_id);
        const cursor = Number(url.searchParams.get("cursor"));
        if (cursor > 0 && !recovered) {
          failures += 1;
          if (mode === "transient" && failures > 1) recovered = true;
          else if (mode === "hung" || mode === "cancel") return new Promise<void>((resolve) => { pending.push(() => route.fulfill({ json: { events: [], next_cursor: cursor } }).then(resolve).catch(resolve)); });
          else if (mode === "malformed") return route.fulfill({ body: "invalid JSON" });
          else return route.fulfill({ status: 503, body: "private upstream failure" });
        }
        const events = [event(1, "assistant_message", { session_id: session.session_id, message: "Your partial design is saved." })];
        if (recovered) events.push(event(2, "failed", {
          session_id: session.session_id, event_id: `${session.session_id}-command:terminal`, status: "failed",
          error: { code: "opencode_command_stalled", message: "Forma Agent could not start or resume this request after three attempts. Check the runtime and retry the request.", correlation_id: "poll-test" },
        }));
        const filtered = events.filter((item) => item.sequence > cursor);
        return route.fulfill({ json: { events: filtered, next_cursor: filtered.at(-1)?.sequence ?? cursor } });
      }
      if (path === `/projects/${projectId}`) return route.fulfill({ status: 404, json: { detail: "No saved project" } });
      unexpected.push(`${method} ${path}`);
      return route.fulfill({ status: 501, json: { detail: "Unmocked endpoint" } });
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    const composer = page.getByPlaceholder("Describe the product, constraints, references, and outputs you need…", { exact: true });
    const stop = page.getByRole("button", { name: "Stop generation", exact: true });
    const check = page.getByRole("button", { name: "Check request status", exact: true });
    await composer.fill("Build a printable hinge");
    await composer.press("Enter");
    await expect(page.getByText("Your partial design is saved.", { exact: true })).toBeVisible();
    if (mode === "cancel") {
      await expect.poll(() => failures).toBe(1);
      await stop.click();
      await Promise.all(pending.map((release) => release()));
      await expect(stop).toHaveCount(0);
      await expect(check).toHaveCount(0);
      await expect.poll(() => cancellations.length).toBe(1);
    } else {
      if (mode !== "transient") {
        await expect(check).toBeVisible({ timeout: 50_000 });
        await expect(stop).toHaveCount(0);
        await expect(page.getByText(/Your partial design is saved[\s\S]*Could not confirm/)).toBeVisible();
        expect(failures).toBe(3);
        if (mode === "reload") {
          await expect.poll(() => JSON.stringify([...chats.values()])).toContain('"openCodeTurn"');
          await page.reload({ waitUntil: "domcontentloaded" });
          await expect(check).toBeVisible();
          await expect(stop).toHaveCount(0);
        }
        recovered = true;
        await check.click();
        await Promise.all(pending.map((release) => release()));
      }
      await expect(page.getByText(/Code: opencode_command_stalled/)).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText(/Your partial design is saved[\s\S]*Code: opencode_command_stalled/)).toBeVisible();
      await expect(stop).toHaveCount(0);
      await expect(check).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Try failed build again", exact: true })).toBeVisible();
    }
    expect(sessions).toHaveLength(1);
    expect(commands).toHaveLength(1); // Reconnection never repeats the POST.
    expect(unexpected).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const source of ["server", "local cache"] as const) {
  test(`legacy welcome stays out of restored chat: ${source}`, async ({ page, baseURL }) => {
    const appOrigin = new URL(baseURL!).origin;
    const chatId = "welcome-history";
    const messages = [
      { id: "real-user", role: "user", content: "hi", status: "idle", timestamp: legacyWelcome.timestamp },
      { id: "real-assistant", role: "assistant", content: "Your saved reply.", status: "success", timestamp: legacyWelcome.timestamp },
      legacyWelcome,
    ];
    const chat = { chat_id: chatId, title: "Welcome history", messages, created_at: legacyWelcome.timestamp };
    const unexpected: string[] = [];
    const errors: string[] = [];
    const savedMessages: Array<Array<{ id: string }>> = [];
    page.on("pageerror", (error) => errors.push(error.message));
    if (source === "local cache") {
      await page.addInitScript(({ chatId, messages }) => {
        localStorage.setItem(`forma.chat.${chatId}`, JSON.stringify(messages));
        localStorage.setItem("forma.chatIndex", JSON.stringify([{
          chatId, title: "Welcome history", projectId: "", projectCount: 0, createdAt: messages[0].timestamp,
        }]));
      }, { chatId, messages });
    }
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === appOrigin && !/^\/api(?:\/|$)/.test(url.pathname)) return route.continue();
      const path = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
      if (path === "/runtime/config") return route.fulfill({ json: runtimeConfig });
      if (path === "/chats") return route.fulfill({ json: source === "server" ? [chat] : [] });
      if (path === `/chats/${chatId}`) {
        if (request.method() === "PUT") {
          savedMessages.push(request.postDataJSON().messages);
          return route.fulfill({ json: { ...chat, ...request.postDataJSON() } });
        }
        return route.fulfill({ status: source === "server" ? 200 : 404, json: source === "server" ? chat : { detail: "Not found" } });
      }
      if (["/projects", "/my/projects"].includes(path)) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
      if (["/a2a/jobs", "/example-project-object-jobs"].includes(path)) return route.fulfill({ json: [] });
      if (path === "/admin/session") return route.fulfill({ json: { is_admin: false } });
      if (path === "/pipeline/steps") return route.fulfill({ json: { steps: [] } });
      if (path === "/video/models") return route.fulfill({ json: { models: [], generation_configured: false } });
      if (path === "/" || request.method() === "OPTIONS") return route.fulfill({ json: { status: "ok" } });
      unexpected.push(`${request.method()} ${path}`);
      return route.fulfill({ status: 501, json: { detail: "Unmocked endpoint" } });
    });
    for (const reload of [false, true]) {
      if (reload) await page.reload({ waitUntil: "domcontentloaded" });
      else await page.goto(`/chat/${chatId}`, { waitUntil: "domcontentloaded" });
      await expect(page.getByText("Your saved reply.", { exact: true })).toBeVisible();
      await expect(page.getByRole("main").getByText("hi", { exact: true })).toBeVisible();
      await expect(page.getByText(legacyWelcome.content, { exact: true })).toHaveCount(0);
    }
    expect(savedMessages.flat().some((message) => message.id === legacyWelcome.id)).toBe(false);
    expect(unexpected).toEqual([]); // Opening history cannot trigger generation.
    expect(errors).toEqual([]);
  });
}
