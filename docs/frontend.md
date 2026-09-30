# Frontend

The frontend is a **Next.js 15** app that visualizes Hardware Intermediate Representation and provides the interactive CAD-style experience.

## Core UI features
- **Prompt input** with optional image upload.
- **Shape clarification** that asks for the intended silhouette or form factor before generation.
- **Example presets** for quick exploration.
- **React Flow schematic** showing components and nets.
- **Vector schematic** rendered from SVG output.
- **BOM & sourcing** table.
- **Assembly instructions** and **mechanical notes** views.
- **Export** of the Hardware Intermediate Representation package as JSON and build instructions as Markdown.
- **3D mechanical scene** for enclosure and component placements.

## Shape and form-factor iteration

The initial chat asks for the system's overall shape, silhouette, or form factor. Answers can describe enclosed products as well as curved, cylindrical, radial, wearable, folded, structural, and open-frame designs; Forma should not assume a rectangular case.

To redesign an existing project's shape without changing its components, open the project, select the **MECH** tab, and describe the new form in project chat—for example, “Keep these components, but change the body to a curved handheld pod with a thumb rest.” The `product.mech` namespace allows mechanical form, dimensions, placement, material, and fabrication details to change while keeping the BOM and electrical connectivity fixed. Use the BOM or WIRE tab when the requested revision should also change components or wiring.

## Primary tabs
The main dashboard exposes several focused views:
- **IMAGE** – project summary plus generated product image, falling back to the uploaded reference image when no generated image is present.
- **BOM** – component list and total cost.
- **MECH** – 3D enclosure + placements (Three.js / React Three Fiber).
- **WIRE** – interactive React Flow wiring view.
- **DOCS** – step-by-step assembly guidance and safety notes.
- **SVG** – static SVG schematic rendering.

## Data flow
The UI communicates with the backend API:
- `GET /` – health check
- `GET /api/components` – component catalog
- `GET /api/projects` – history of generated projects
- `POST /api/generate` – run the agent pipeline

## Installable viewer package

The reusable project browser and project detail viewer are packaged for publication as `@isayahc/forma-gui`:

```bash
npm install @isayahc/forma-gui
```

Import `@isayahc/forma-gui/styles.css` once, then configure `FormaApiClient` with a `baseUrl` and optional `getHeaders` callback. The callback is the authentication boundary for hosted deployments; local single-user APIs can omit it. The package exposes typed `FormaProjectBrowser`, `FormaProjectDetail`, and canonical project contracts without importing Clerk, provider credentials, routing, Tailwind, React Flow, or Three.js.

The first-party project and my-projects pages consume the published package. Run `npm run build` and `npm test` from `packages/forma-gui` before publishing a semver release.

If the backend is offline, the UI can still load example JSONs from `apps/web/public/examples/`.

## Deep links
You can load an example directly:
- `http://localhost:3000/?example=pocket_mp3_player`

You can also preselect a tab:
- `http://localhost:3000/?example=pocket_mp3_player&tab=mech`

## Where to look
- `apps/web/app/page.tsx` – main UI, React Flow rendering, and tab layouts
- `apps/web/public/examples/` – example IR JSON files
- `apps/web/app/globals.css` – styling and theming
- `apps/web/components/mechanical-scene.tsx` – 3D mechanical viewer

## Rendering details
The schematic view maps:
- **Components → Nodes**
- **ConnectionNet → Edges**
- **Net type → Color coding**

This makes it easy to visualize power, ground, I2C, SPI, and other signal types at a glance.
