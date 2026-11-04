"""Authorized canonical artifact adapters shared by application HTTP and MCP."""

import hashlib
import json

from forma_core.workspaces.projects.inspection import ArtifactReader, inspect_project
from forma_core.workspaces.projects.manifest import ProjectManifest, ProjectArtifactReference, rewrite_artifact_paths, redact_project_secrets


def revision_project(revision, project_id: str, owner_user_id: str) -> tuple[ProjectManifest, ArtifactReader]:
    """Resolve only server-owned artifact declarations, never caller file paths."""
    from forma_core.persistence.project_artifacts import ProjectArtifactStorage
    from apps.api.component_assets import project_library
    references = []
    readers = {}
    virtual_payloads = {}
    state_snapshots = set()
    transformations = {}
    replacements = {}
    storage = ProjectArtifactStorage()
    state = revision.state
    cad = state.cad_model if isinstance(state.cad_model, dict) else {}
    path_by_hash = {}
    for field in ("path", "preview_path", "model_source_path", "feature_tree_path"):
        value = cad.get(field)
        if field == "path":
            digest = cad.get("sha256")
        elif field == "preview_path":
            digest = cad.get("exports", {}).get("stl", {}).get("sha256")
        else:
            role = "model_source" if field == "model_source_path" else "feature_tree"
            digest = cad.get("source_artifacts", {}).get(role, {}).get("sha256")
        if value and digest:
            path_by_hash[digest] = value
    virtual = {"project-state": state.model_dump(mode="json"),
               "bom": [item.model_dump(mode="json") for item in state.bom],
               "wiring": [item.model_dump(mode="json") for item in state.nets],
               "mechanical": state.mechanical.model_dump(mode="json") if state.mechanical else None,
               "assembly": [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in state.assembly]}
    generation = state.assembly_metadata.get("generation_run", {})
    records = generation.get("records", {}) if isinstance(generation, dict) else {}
    records = records.values() if isinstance(records, dict) else []
    stage_records = {record.get("artifact_id"): record for record in records if isinstance(record, dict)}
    for index, artifact in enumerate(revision.artifacts):
        media_type = artifact.media_type or "application/octet-stream"
        suffix = {"model/step": ".step", "model/stl": ".stl", "model/obj": ".obj", "model/3mf": ".3mf",
                  "text/x-python": ".py", "application/json": ".json",
                  "application/vnd.forma.hardware-ir+json": ".json"}.get(media_type, ".bin")
        path = f"artifacts/{index:04d}{suffix}"
        checksum = str(artifact.checksum or "").removeprefix("sha256:") or None
        if artifact.kind in virtual:
            # Bind virtual snapshots only after every CAD path replacement is
            # known. They cross the same portability/privacy boundary as IR.
            virtual_payloads[path] = virtual[artifact.kind]
            if artifact.kind == "project-state":
                state_snapshots.add(path)
        elif artifact.kind.startswith("generation-stage:"):
            record = stage_records.get(artifact.artifact_id)
            saved_artifact = record.get("artifact") if isinstance(record, dict) else None
            if not isinstance(saved_artifact, dict) or saved_artifact.get("artifact_id") != artifact.artifact_id:
                raise ValueError("Canonical generation stage has no matching saved output.")
            original_bytes = json.dumps(record.get("output"), sort_keys=True, separators=(",", ":"), default=str).encode()
            original_checksum = "sha256:" + hashlib.sha256(original_bytes).hexdigest()
            if original_checksum != artifact.checksum or original_checksum != record.get("output_fingerprint"):
                raise ValueError("Canonical generation stage output failed its saved checksum.")
            virtual_payloads[path] = record.get("output")
            transformations[path] = {"source_artifact_id": artifact.artifact_id, "source_checksum": original_checksum,
                                     "transformation": "redacted_portable_json"}
        elif artifact.kind == "component.cad":
            metadata = artifact.metadata
            def read_component(_reference, metadata=metadata):
                library = project_library(project_id, owner_user_id)
                return library.content(metadata["asset_id"], metadata["version"], metadata["representation"])
            readers[path] = read_component
        else:
            readers[path] = lambda reference: storage.get(project_id, reference.sha256, reference.media_type).content
        references.append(ProjectArtifactReference(path=path, sha256=checksum, media_type=media_type,
                                                  provenance=transformations.get(path)))
        if checksum in path_by_hash:
            replacements[path_by_hash[checksum]] = path
    portable_state = redact_project_secrets(rewrite_artifact_paths(state.model_dump(mode="json"), replacements))
    metadata = portable_state.get("assembly_metadata", {})
    exported_run = metadata.pop("generation_run", None)
    if exported_run is not None:
        # Saved stage fingerprints describe the original canonical output.
        # Redaction and path rewriting can change those bytes. Preserve their
        # provenance while preventing an imported design from replaying an
        # active retry cache under the old fingerprints.
        metadata["generation_history"] = {"execution_authority": "historical_only",
                                          "source_revision_id": str(revision.revision_id), "run": exported_run}
    for reference in references:
        if reference.path in virtual_payloads:
            payload = (portable_state if reference.path in state_snapshots else
                       redact_project_secrets(rewrite_artifact_paths(virtual_payloads[reference.path], replacements)))
            content = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            reference.sha256 = hashlib.sha256(content).hexdigest()
            reference.size_bytes = len(content)
            readers[reference.path] = lambda _reference, content=content: content
    manifest = ProjectManifest(project_id=project_id, project_ir=portable_state,
                               artifacts=references, revision=str(revision.revision_id),
                               assumptions=list(getattr(revision, "assumptions", [])))
    return manifest, lambda reference: readers[reference.path](reference)


def inspect_revision(revision, project_id: str, owner_user_id: str) -> dict[str, object]:
    manifest, reader = revision_project(revision, project_id, owner_user_id)
    return inspect_project(manifest, read_artifact=reader).model_dump(mode="json")
