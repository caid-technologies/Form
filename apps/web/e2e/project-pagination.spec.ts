import { expect, test, type Page } from "@playwright/test";

type Project = {
  project_id: string;
  title: string;
  description: string;
};

const projects: Project[] = Array.from({ length: 7 }, (_, index) => ({
  project_id: `project-${index + 1}`,
  title: `Project ${index + 1}`,
  description: "A test project",
}));

function pageResponse(url: URL) {
  const offset = Number(url.searchParams.get("offset") || 0);
  const limit = Number(url.searchParams.get("limit") || 6);
  return {
    items: projects.slice(offset, offset + limit),
    total: projects.length,
    limit,
    offset,
    has_more: offset + limit < projects.length,
  };
}

async function mockProjectList(page: Page, path: string) {
  const endpoint = new RegExp(
    `^https?://(?:localhost|127\\.0\\.0\\.1):8000/(?:api/)?${path}(?:\\?|$)`,
  );
  await page.route(endpoint, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(pageResponse(new URL(route.request().url()))),
    });
  });
}

async function openRoute(page: Page, path: string) {
  try {
    await page.goto(path, { waitUntil: "domcontentloaded", timeout: 120_000 });
  } catch (error) {
    // Next dev can detach a frame while compiling a freshly requested route.
    if (!String(error).includes("ERR_ABORTED")) throw error;
    await page.goto(path, { waitUntil: "domcontentloaded", timeout: 120_000 });
  }
}

function projectCards(page: Page) {
  return page.locator("section.forma-gui-browser[aria-busy='false'] .forma-gui-card:not(.forma-gui-card-skeleton)");
}

test.describe("project pagination", () => {
  test("community keeps six fetched projects per page", async ({ page }) => {
    await mockProjectList(page, "projects");
    await openRoute(page, "/projects");

    await expect(page.getByRole("main").getByRole("heading", { name: "Community" })).toBeVisible();
    await expect(projectCards(page)).toHaveCount(6);
    await expect(page.getByText("Page 1 of 2")).toBeVisible();

    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(projectCards(page)).toHaveCount(1);
    await expect(page.getByText("Page 2 of 2")).toBeVisible();
  });

  test("my projects uses the same page size and search contract", async ({ page }) => {
    const requests: string[] = [];
    await page.route(/^https?:\/\/(?:localhost|127\.0\.0\.1):8000\/(?:api\/)?my\/projects(?:\?|$)/, async (route) => {
      requests.push(route.request().url());
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(pageResponse(new URL(route.request().url()))),
      });
    });
    await openRoute(page, "/my-projects");

    await expect(page.getByRole("main").getByRole("heading", { name: "My projects" })).toBeVisible();
    await expect(projectCards(page)).toHaveCount(6);
    await expect(page.getByText("Page 1 of 2")).toBeVisible();

    await page.getByPlaceholder("Search projects").fill("controller");
    await expect.poll(() => requests.some((url) => url.includes("q=controller"))).toBe(true);
  });
});


test.describe("gallery loading performance", () => {
  test("a cached page stays mounted while its refresh is pending", async ({ page }) => {
    let firstPageReads = 0;
    let release!: () => void;
    const pendingRefresh = new Promise<void>((resolve) => { release = resolve; });
    await page.route(/^https?:\/\/(?:localhost|127\.0\.0\.1):8000\/(?:api\/)?projects(?:\?|$)/, async (route) => {
      const url = new URL(route.request().url());
      if (Number(url.searchParams.get("offset") || 0) === 0 && ++firstPageReads > 1) {
        await pendingRefresh;
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(pageResponse(url)) });
    });
    try {
      await openRoute(page, "/projects");
      await expect(projectCards(page)).toHaveCount(6);
      await page.getByRole("button", { name: "Next", exact: true }).click();
      await expect(projectCards(page)).toHaveCount(1);
      await page.getByRole("button", { name: "Previous", exact: true }).click();
      await expect.poll(() => firstPageReads).toBe(2);
      await expect(projectCards(page)).toHaveCount(6);
      await expect(page.locator(".forma-gui-card-skeleton")).toHaveCount(0);
      await expect(page.getByText("Page 1 of 2")).toBeVisible();
    } finally {
      release();
    }
  });

  test("a fast image is displayed while a sibling image summary is pending", async ({ page }) => {
    const counts = new Map<string, number>();
    let release!: () => void;
    const slow = new Promise<void>((resolve) => { release = resolve; });
    await mockProjectList(page, "projects");
    await page.route(/\/projects\/project-\d+\/image-summary$/, async (route) => {
      const id = route.request().url().match(/project-\d+/)![0];
      counts.set(id, (counts.get(id) || 0) + 1);
      if (id === "project-2") await slow;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({
        product_image_url: `https://images.example.test/${id}.svg`,
      }) });
    });
    await page.route("https://images.example.test/**", (route) => route.fulfill({
      contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>',
    }));
    try {
      await openRoute(page, "/projects");
      await expect(page.locator('img[src="https://images.example.test/project-1.svg"]')).toBeVisible();
      await expect.poll(() => counts.get("project-2")).toBe(1);
      await expect(page.locator('img[src="https://images.example.test/project-2.svg"]')).toHaveCount(0);
      release();
      await expect(page.locator('img[src="https://images.example.test/project-2.svg"]')).toBeVisible();
      expect(counts.get("project-2")).toBe(1);
    } finally {
      release();
    }
  });

  test("a failed uncached search shows an error, not stale results or an empty success", async ({ page }) => {
    await page.route(/^https?:\/\/(?:localhost|127\.0\.0\.1):8000\/(?:api\/)?projects(?:\?|$)/, async (route) => {
      const url = new URL(route.request().url());
      await route.fulfill(url.searchParams.has("q")
        ? { status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Gallery temporarily unavailable" }) }
        : { contentType: "application/json", body: JSON.stringify(pageResponse(url)) });
    });
    await openRoute(page, "/projects");
    await expect(projectCards(page)).toHaveCount(6);
    await page.getByPlaceholder("Search projects").fill("offline");
    await expect(page.getByText("Projects unavailable", { exact: true })).toBeVisible();
    await expect(page.locator(".forma-gui-card:not(.forma-gui-card-skeleton)")).toHaveCount(0);
  });
});
