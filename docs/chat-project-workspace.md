# Shared chat project workspace

The workspace shares one project pane across the conversation, full-screen view, and saved version previews.

## Interaction

On wide containers, the conversation and current project use independent, resizable panes. On narrow containers, Chat / Project controls switch the visible pane while preserving the composer draft. Full screen expands the same project subtree; Escape returns to the prior view. The project remains available without scrolling to the end of the chat.

Completed assistant messages render lightweight project-reference cards. When a response contains a saved revision ID, **View this revision** opens that exact snapshot in the shared pane. Cards referencing another project use `/project/{project_id}?revision={revision_id}`. Older messages without a revision ID still open the latest project and say that a version was not recorded for that message.

## Version history

The **History** button beside the project title opens a list of saved versions, newest first. Each row shows a version number, saved time, and change summary. Selecting a row displays its saved overview, image, CAD, BOM, wiring, assembly, and downloads. The header shows the selected version; **Return to latest** fetches the current project again so updates made while browsing history are included.

History sits beside the preview when the project pane is wide enough. In a narrow pane it overlays the preview and closes when a version is selected. The `revision` query parameter preserves a selection across reloads, including when opening a chat on mobile. Chat remains bound to the latest project while an older version is displayed.

The API reads immutable `project_revisions` records. `GET /projects/{project_id}/history` accepts `limit` (1–50) and `before` (exclusive version number). `GET /projects/{project_id}/history/{revision_id}` returns the exact snapshot. Both require the active project's owner; making a project public does not expose its history. Missing versions display an error instead of falling back to current state. Legacy projects with no saved revisions show an empty state.

Snapshot image hydration only refreshes keys already recorded in that version; it never discovers a newer image. Saved STEP downloads use `/projects/{project_id}/history/{revision_id}/cad/{sha256}`, checking the owned snapshot and the artifact checksum. JSON and documentation exports use the selected snapshot. New mesh exports and G-code are available from the latest project.

No database migration or backfill is required. New canonical revisions carry their UUID in project metadata, and OpenCode completion/recovery preserves that UUID on the corresponding assistant message.

## Resource lifecycle

Only one project subtree is mounted per active conversation. Closing the pane or switching to Chat on a narrow screen unmounts it; switching conversations replaces the previous session. Full-screen presentation does not duplicate or remount the viewer. Existing tab selection mounts only the active tab. Cards contain IDs and text, never mesh copies or a viewer.

CAD file fetches use an AbortController and cleanup aborts them. Non-cancellable SDK import/mesh calls are guarded so a stale result cannot update the panel or start the next import stage. Parent-held project payloads and SDK-owned GPU disposal are not claimed to have been eliminated or benchmarked.

## Tests

Run the focused state/source tests with Node 22: `node --experimental-strip-types --test test/project-history.test.ts test/chat-project-layout.test.ts test/chat-project-integration.test.ts test/cad-model.test.ts` from apps/web. Run the Chromium layout suite with `npx playwright test --config playwright.workspace.config.mjs`. The browser fixture imports the production layout and uses an instrumented stand-in viewer to check active instance counts, not real CAD GPU memory.

`npx playwright test e2e/version-history.spec.ts` covers exact message links, snapshot downloads, a newer version arriving during preview, request races, missing-version retry, reloads, and mobile navigation against mocked API responses. Backend coverage is `python -m unittest tests.projects.test_project_history tests.opencode.test_history`, including SQLite pagination, owner isolation, immutable images, STEP authorization/integrity, Supabase query filters, and recovered message revision IDs.

The read-only pull-request workflow targets main and runs full frontend typechecking plus the focused tests. No generated patch or integration helper is needed to build this UI.
