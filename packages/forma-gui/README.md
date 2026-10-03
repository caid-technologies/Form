# `@isayahc/forma-gui`

Reusable React components for browsing and viewing Forma hardware projects. The package is intentionally a browser-safe viewer surface: it does not import Clerk, provider SDKs, backend credentials, or application routing.

## Install

```bash
npm install @isayahc/forma-gui
```

React 18 or 19 and React DOM 18 or 19 are peer dependencies.

## Usage

Import the package CSS once from the application entry point:

```tsx
import {
  FormaApiClient,
  FormaProjectBrowser,
  FormaProjectDetail,
} from "@isayahc/forma-gui";
import "@isayahc/forma-gui/styles.css";

const forma = new FormaApiClient({
  baseUrl: process.env.NEXT_PUBLIC_FORMA_API_URL,
  getHeaders: async () => {
    const token = await getHostedSessionToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  },
});

export function Projects() {
  return <FormaProjectBrowser client={forma} onOpenProject={(id) => console.log(id)} />;
}

export function Project({ projectId }: { projectId: string }) {
  return <FormaProjectDetail client={forma} projectId={projectId} />;
}
```

`baseUrl` may be an origin such as `https://forma.example.com` or an API root ending in `/api`. The client normalizes both forms. For local single-user APIs, omit `getHeaders`; no Clerk or hosted authentication assumption is built into the package.

## API surface

- `FormaApiClient.listProjects()` loads `/projects` or `/my/projects` with search and pagination.
- `FormaApiClient.getProject(projectId)` loads the canonical project response from `/projects/{id}`.
- `FormaApiClient.getImageSummary(projectId)` loads `/projects/{id}/image-summary`.
- `FormaProjectBrowser` supports controlled project lists or client-backed loading, search, pagination, open/save/remix callbacks, and loading/empty/unauthorized/unavailable states.
- `FormaProjectDetail` renders overview, BOM, validation, schematic, mechanical, assembly, and artifact sections. Use `renderSection` when a host needs to replace a section while keeping the typed loading and error shell.
- `FormaApiError` exposes `status`, `code`, `correlationId`, and `unauthorized` without retaining raw response text.

The components use CSS variables with `--forma-*` fallbacks. Override the `--forma-gui-*` variables or the corresponding application variables to match a host theme. The package does not include Tailwind, React Flow, Three.js, image hosting, or router CSS. Schematic SVG is displayed as an image data URL rather than injected into the DOM.

## Security boundary

Keep API tokens in the host application's request callback. Do not put provider credentials, server-only environment variables, or long-lived tokens in project data, local storage, or public build-time variables. For hosted deployments, return short-lived session headers from `getHeaders`; for local deployments, configure a same-origin or local API base URL.

## Publishing

From `packages/forma-gui`:

```bash
npm run pack:check
npm publish --access public
```

Use semver for releases. `dist/` is generated and included in the npm tarball; it is not required in the source repository. A companion `npx` launcher is intentionally deferred until a command-line workflow has a separate stable contract.
