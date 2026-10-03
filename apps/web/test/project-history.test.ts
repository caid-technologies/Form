import assert from "node:assert/strict";
import { test } from "node:test";
import { mergeRevisionPages, parseRevisionPage, parseRevisionSnapshot, revisionFromProject, revisionId } from "../lib/project-history.ts";
import { linkedProjectPath } from "../lib/chat-project-layout.ts";
import { nativeStepDownloadPath } from "../lib/cad-model.ts";

const project = "11111111-1111-4111-8111-111111111111";
const ids = ["22222222-2222-4222-8222-222222222221", "22222222-2222-4222-8222-222222222222", "22222222-2222-4222-8222-222222222223"];
const summary = (version: number) => ({ revision_id: ids[version - 1], revision: version, parent_revision: version > 1 ? version - 1 : null, created_at: "2026-09-21T11:00:00Z", title: "Robot arm", summary: `Change ${version}` });

test("overlapping history pages stay unique and ordered by version", () => {
  assert.deepEqual(mergeRevisionPages([summary(3), summary(2)], [summary(2), summary(1)]).map((item) => item.revision), [3, 2, 1]);
});

test("a snapshot response must match both the requested project and exact revision", () => {
  const value = { ...summary(1), project_id: project, project_ir: { components: [], overview: { title: "Base" } } };
  assert.equal(parseRevisionSnapshot(value, project, ids[0]).project_ir.overview.title, "Base");
  assert.throws(() => parseRevisionSnapshot(value, project, ids[1]));
  assert.throws(() => parseRevisionSnapshot(value, "another-project", ids[0]));
  assert.throws(() => parseRevisionSnapshot({ ...value, project_ir: null }, project, ids[0]));
});

test("invalid history responses cannot be presented as an empty or latest version", () => {
  assert.throws(() => parseRevisionPage({ items: [] }, project));
  assert.throws(() => parseRevisionPage({ project_id: project, items: [summary(1)], latest_revision: -1, next_before: null }, project));
  assert.equal(parseRevisionPage({ project_id: project, items: [], latest_revision: null, next_before: null }, project).items.length, 0);
});

test("revision links and STEP downloads remain attached to the saved version", () => {
  assert.equal(linkedProjectPath(project, ids[0]), `/project/${project}?revision=${ids[0]}`);
  assert.equal(nativeStepDownloadPath({ projectId: project, sha256: "a".repeat(64) }, ids[0]), `/projects/${project}/history/${ids[0]}/cad/${"a".repeat(64)}`);
  assert.equal(revisionFromProject({ assembly_metadata: { canonical_revision_id: ids[1] } }).revisionId, ids[1]);
  assert.equal(revisionFromProject({ assembly_metadata: { revision: 2 } }).revisionId, null);
  assert.equal(revisionId("untracked-old-result"), null);
});
