import { build } from "esbuild";
import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { join } from "node:path";

const directory = join(process.cwd(), ".chat-workspace-test");
await mkdir(directory, { recursive: true });
await build({
  entryPoints: ["test/fixtures/chat-project-workspace.tsx"],
  bundle: true, platform: "browser", jsx: "automatic", sourcemap: true,
  outfile: join(directory, "fixture.js"),
  define: { "process.env.NODE_ENV": '"development"', "process.env": '{"FORMA_AUTH_MODE":"local"}' },
  alias: { "@clerk/nextjs": join(process.cwd(), "test/fixtures/clerk-local.ts") },
});
const html = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forma workspace UI test</title><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>';
const files = new Map([["/fixture.js", "text/javascript"], ["/fixture.css", "text/css"], ["/fixture.js.map", "application/json"]]);
const server = createServer(async (request, response) => {
  if (request.url === "/") { response.writeHead(200, { "Content-Type": "text/html" }); response.end(html); return; }
  const mime = files.get(request.url);
  if (!mime) { response.writeHead(404); response.end(); return; }
  try {
    const content = await readFile(join(directory, request.url.slice(1)));
    response.writeHead(200, { "Content-Type": mime }); response.end(content);
  } catch { response.writeHead(404); response.end(); }
});
server.listen(4175, "127.0.0.1");
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => server.close(() => process.exit(0)));
