import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { FormaApiClient, FormaApiError, FormaProjectBrowser, FormaProjectDetail } from "../dist/index.js";

test("FormaApiClient normalizes an origin and supplies request headers", async () => {
  const calls = [];
  const client = new FormaApiClient({
    baseUrl: "https://forma.example.test/",
    getHeaders: ({ path, method }) => ({
      Authorization: `Bearer ${path}:${method}`,
      "X-Client": "forma-gui-test",
    }),
    fetcher: async (url, init) => {
      calls.push({ url: String(url), headers: new Headers(init?.headers) });
      return new Response(JSON.stringify({ items: [{ project_id: "p-1", title: "Monitor" }], total: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });

  const result = await client.listProjects({ search: "monitor", limit: 6, offset: 12 });

  assert.deepEqual(result.items, [{ project_id: "p-1", title: "Monitor" }]);
  assert.equal(result.total, 1);
  assert.equal(calls[0].url, "https://forma.example.test/api/projects?q=monitor&limit=6&offset=12");
  assert.equal(calls[0].headers.get("authorization"), "Bearer /projects?q=monitor&limit=6&offset=12:GET");
  assert.equal(calls[0].headers.get("x-client"), "forma-gui-test");
});

test("FormaApiClient preserves canonical error metadata without exposing raw error text", async () => {
  const client = new FormaApiClient({
    baseUrl: "/api",
    fetcher: async () => new Response(JSON.stringify({ detail: "provider key secret should not reach the UI" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    }),
  });

  await assert.rejects(
    client.getProject("project-1"),
    (error) => {
      assert.ok(error instanceof FormaApiError);
      assert.equal(error.status, 500);
      assert.equal(error.code, null);
      assert.equal(error.correlationId, null);
      assert.equal(error.message, "Forma could not complete that request.");
      return true;
    },
  );
});

test("FormaApiClient does not trust an unstructured error message", async () => {
  const client = new FormaApiClient({
    fetcher: async () => new Response(JSON.stringify({ detail: { message: "provider response with a secret" } }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    }),
  });

  await assert.rejects(
    client.getProject("project-1"),
    (error) => {
      assert.equal(error.message, "Forma could not complete that request.");
      return true;
    },
  );
});

test("FormaApiClient exposes canonical unauthorized metadata", async () => {
  const client = new FormaApiClient({
    fetcher: async () => new Response(JSON.stringify({
      detail: {
        code: "project_access_denied",
        message: "You do not have access to this project.",
        correlation_id: "err_123",
      },
    }), { status: 403, headers: { "Content-Type": "application/json" } }),
  });

  await assert.rejects(
    client.getProject("private-project"),
    (error) => {
      assert.ok(error instanceof FormaApiError);
      assert.equal(error.unauthorized, true);
      assert.equal(error.code, "project_access_denied");
      assert.equal(error.correlationId, "err_123");
      assert.equal(error.message, "You do not have access to this project.");
      return true;
    },
  );
});

test("the published component entry point renders browser and detail views", () => {
  const project = {
    project_id: "project-1",
    title: "Pocket monitor",
    parts_count: 2,
    project_ir: {
      overview: { title: "Pocket monitor", description: "A low-voltage field monitor." },
      components: [
        { name: "Controller", category: "microcontroller", quantity: 1, unit_price: 12 },
      ],
      validation: { critical: [], warning: [], info: [] },
    },
  };

  const browser = renderToStaticMarkup(React.createElement(FormaProjectBrowser, {
    projects: [project],
    onOpenProject: () => {},
  }));
  const detail = renderToStaticMarkup(React.createElement(FormaProjectDetail, {
    project,
    activeSection: "bom",
  }));

  assert.match(browser, /Pocket monitor/);
  assert.match(detail, /Controller/);
  assert.match(detail, /Bill of materials/);
});
