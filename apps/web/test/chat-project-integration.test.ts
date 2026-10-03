import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const workspace = readFileSync(new URL("../app/forma-workspace.tsx", import.meta.url), "utf8");
const home = readFileSync(new URL("../app/forma-workspace/home-chat-view.tsx", import.meta.url), "utf8");
const messages = readFileSync(new URL("../app/forma-workspace/conversation-message-list.tsx", import.meta.url), "utf8");
const exportsPanel = readFileSync(new URL("../app/forma-workspace/project-exports-panel.tsx", import.meta.url), "utf8");
const projectPanels = readFileSync(new URL("../app/forma-workspace/project-detail-panels.tsx", import.meta.url), "utf8");
const chatProjectLayout = readFileSync(new URL("../app/forma-workspace/chat-project-layout.tsx", import.meta.url), "utf8");
const generationModeSelector = readFileSync(new URL("../app/forma-workspace/generation-mode-selector.tsx", import.meta.url), "utf8");
const cad = readFileSync(new URL("../app/forma-workspace/cad-model-panel.tsx", import.meta.url), "utf8");
const chat = workspace.slice(workspace.indexOf("function ChatWorkspace("), workspace.indexOf("function ChatProjectArtifact("));
const surface = workspace.slice(workspace.indexOf("function ChatProjectArtifact("), workspace.indexOf("function ProjectWorkspacePanel("));

test("main project chat moves exactly one artifact outside the conversation scroller", () => {
  assert.match(chat, /<ChatProjectLayout/);
  assert.ok(chat.includes("project={(chatAvailable || readOnly) && projectId ? ("));
  assert.equal((chat.match(/<ChatProjectArtifact\b/g) || []).length, 1);
  assert.ok(chat.indexOf("<ChatProjectArtifact") < chat.indexOf("ref={containerRef}"));
  assert.match(chat, /<ConversationMessageList/);
});
test("main home chat uses the same workspace without an inline artifact", () => {
  assert.ok(home.includes("project={started ? projectArtifact : null}"));
  assert.doesNotMatch(home, /^\s*\{projectArtifact\}\s*$/m);
  assert.match(home, /<ConversationMessageList/);
  assert.ok(workspace.includes("projectArtifactId={inlineChatProjectId}"));
});
test("both main chat entry points share the lightweight message card renderer", () => {
  assert.ok(messages.includes("<ProjectUpdateCard message={message} />"));
  assert.doesNotMatch(messages, /<ChatProjectArtifact|<CadModelPanel|<OpenCadViewport/);
});
test("exports live in the project tab bar and own all project download actions", () => {
  assert.ok(workspace.includes('{ id: "exports", label: "Exports", icon: Download }'));
  assert.ok(workspace.includes('case "exports":'));
  assert.ok(workspace.includes("<ProjectExportsPanel"));
  assert.ok(exportsPanel.includes("Project JSON"));
  assert.ok(exportsPanel.includes("Build documentation"));
  assert.ok(exportsPanel.includes('aria-haspopup="menu"'));
  assert.ok(exportsPanel.includes('aria-label="Download CAD format"'));
  assert.ok(exportsPanel.includes(">STEP</span>"));
  assert.ok(exportsPanel.includes('["stl", "3mf", "obj"]'));
  assert.ok(exportsPanel.includes("Download G-code"));
  assert.ok(exportsPanel.includes("left-0 right-auto"));
  assert.ok(exportsPanel.includes("sm:left-auto sm:right-0"));
  assert.doesNotMatch(cad, /Download STEP|\\bStepDownload\\b/);
  assert.doesNotMatch(projectPanels, /docs-export-menu/);
  assert.doesNotMatch(chatProjectLayout, /surfaceTab|setSurfaceTab|>Exports<|>Project</);
});

test("surface preserves project tabs without the old fixed-height inline card", () => {
  assert.match(surface, /<ChatProjectSurface/);
  assert.match(surface, /<ProjectWorkspacePanel/);
  assert.doesNotMatch(surface, /70dvh|min-h-\[540px\]|setFullScreen|scrollableVerticalParent/);
});
test("main authoring, read-only, submit, stop and retry behavior remains wired", () => {
  assert.ok(chat.includes("onSubmit={onSubmit}"));
  assert.ok(chat.includes("if (canStop) onStop();"));
  assert.ok(chat.includes("else onRetryFailedBuild();"));
  assert.ok(chat.includes('assistantLabel={authoringActive ? "OpenCode" : "Forma"}'));
  assert.ok(chat.includes("canEdit={chatAvailable && Boolean(onRenameTitle)}"));
  assert.ok(chat.includes("!chatAvailable && !readOnly"));
  assert.match(home, /className=\{started\s*\? "relative/);
  assert.ok(home.includes("onSubmit={onSubmit}"));
});
test("home chat reuses one generation mode selector in every auth/authoring state", () => {
  assert.equal((home.match(/<GenerationModeSelector\b/g) || []).length, 1);
  assert.doesNotMatch(home, /<select[\s\S]{0,400}Regular/);
  assert.ok(generationModeSelector.includes('<span className="sr-only">Generation mode</span>'));
  assert.ok(generationModeSelector.includes('<option value="regular">Regular</option>'));
  assert.ok(generationModeSelector.includes('<option value="progressive">Progressive</option>'));
  assert.doesNotMatch(home, /!authoringActive[\s\S]{0,300}GenerationModeSelector/);
});

test("fresh home chat stays writable for signed-out visitors and authenticates before submit", () => {
  assert.doesNotMatch(home, /HostedChatMaintenance/);
  assert.match(home, /\{\(!readOnly \|\| !started\) && \(\s*<form/);

  const gatherContext = workspace.slice(
    workspace.indexOf("const submitGatherContext = async"),
    workspace.indexOf("const handleGatherContext ="),
  );
  const generate = workspace.slice(
    workspace.indexOf("const handleGenerate = async"),
    workspace.indexOf("const handleProjectChatGenerate ="),
  );

  assert.ok(gatherContext.indexOf("requireSignedInForGeneration") < gatherContext.indexOf("requireHostedChatEnabled"));
  assert.ok(generate.indexOf("requireSignedInForGeneration") < generate.indexOf("requireHostedChatEnabled"));

  const startNewChat = workspace.slice(
    workspace.indexOf("const startNewProjectChat ="),
    workspace.indexOf("const openChatItem ="),
  );
  assert.doesNotMatch(startNewChat, /requireHostedChatEnabled/);
  assert.ok(workspace.includes('const newChatDisabled = chatAccessState !== "ready" ||'));
});

test("CAD teardown cancels browser downloads and guards non-cancellable SDK work", () => {
  assert.match(cad, /controller\.abort\(\)/);
  assert.ok(cad.includes("fetch(url, { signal })"));
  assert.equal((cad.match(/signal\.throwIfAborted\(\)/g) || []).length, 2);
  assert.ok(cad.includes("if (cancelled) return;"));
});
