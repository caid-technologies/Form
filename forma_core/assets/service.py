"""Library-first component resolution, integrity checking, and pinned reuse."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from forma_core.assets.models import AssetScope, AssetVersion, AttachRequest, Registration, digest, normalized
from forma_core.assets.validation import inspect_step
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, ProjectArtifactStorageError

logger = logging.getLogger(__name__)


class AssetRecoveryRequired(ValueError):
    pass


class ComponentAssetLibrary:
    def __init__(self, repository, storage: ProjectArtifactStorage, scope: AssetScope):
        self.repository, self.storage, self.scope = repository, storage, scope
        self.blob_scope = "component-library:" + scope.key
        self.metrics = {"hits": 0, "misses": 0, "source_calls": 0, "blob_writes": 0,
                        "registrations": 0, "integrity_failures": 0}

    def _event(self, event: str, **details):
        self.metrics[event] += 1
        logger.info("component_asset_library %s", event, extra={"asset_event": event, **details})

    def inspect(self, asset_id: str, version: str) -> AssetVersion:
        row = self.repository.get(self.scope.key, asset_id, version)
        if row is None:
            # Identical response for unknown and unauthorized records.
            raise PermissionError("The component asset is unavailable in this scope.")
        return AssetVersion.model_validate(row)

    def content(self, asset_id: str, version: str, name: str) -> bytes:
        asset = self.inspect(asset_id, version)
        rep = next((r for r in asset.registration.representations if r.name == name), None)
        if rep is None:
            raise ValueError("Unknown asset representation.")
        try:
            data = self.storage.get(self.blob_scope, rep.sha256, rep.media_type).content
            if data is None or len(data) != rep.size_bytes or hashlib.sha256(data).hexdigest() != rep.sha256:
                raise ValueError("Checksum mismatch")
        except (FileNotFoundError, ProjectArtifactStorageError, ValueError) as exc:
            self._event("integrity_failures", asset_id=asset_id)
            raise AssetRecoveryRequired("Stored component bytes are missing, corrupt, or unavailable. Explicitly re-register the original bytes or source a replacement; existing project pins stay unchanged.") from exc
        return data

    def verify(self, asset: AssetVersion):
        for rep in asset.registration.representations:
            self.content(asset.asset_id, asset.version, rep.name)

    def search(self, identity, *, allow_approximate=False) -> dict:
        candidates = [AssetVersion.model_validate(r) for r in self.repository.find(self.scope.key, identity.key)]
        if not candidates:
            family = self.repository.find(self.scope.key, identity.family_key, family=True)
            if len(family) > 100:
                return {"status": "needs_clarification", "message": "Too many aliases; supply the canonical part number."}
            candidates = [AssetVersion.model_validate(r) for r in family
                          if normalized(identity.part_number) in {normalized(a) for a in r["registration"]["aliases"]}]
        usable = [a for a in candidates if a.validation["status"] == "validated"
                  and (allow_approximate or not a.registration.approximate)]
        # Several models for one part require a deliberate version choice, never an arbitrary latest model.
        if len(usable) == 1:
            try:
                self.verify(usable[0])
            except AssetRecoveryRequired as exc:
                return {"status": "recovery_required", "message": str(exc), "candidates": [usable[0].model_dump(mode="json")]}
            self._event("hits", asset_id=usable[0].asset_id)
            return {"status": "hit", "match_evidence": "Exact normalized manufacturer, part or registered alias, revision and all variants",
                    "asset": usable[0].model_dump(mode="json")}
        if candidates:
            return {"status": "needs_clarification", "candidates": [a.model_dump(mode="json") for a in candidates],
                    "message": "Choose an explicit version, revalidate, or explicitly accept approximate geometry.",
                    "truncated": len(candidates) > 100}
        self._event("misses")
        return {"status": "miss", "next_action": "cad_sourcing_required", "identity": identity.model_dump(mode="json")}

    def resolve(self, identity, source=None, *, allow_approximate=False) -> dict:
        """#530 plugs its sourcing callback here; a hit never invokes it."""
        result = self.search(identity, allow_approximate=allow_approximate)
        if result["status"] != "miss" or source is None:
            return result
        self._event("source_calls")
        registration, contents = source(identity)
        if registration.identity.key != identity.key:
            raise ValueError("The sourcing result does not match the requested identity.")
        self.register(registration, contents)
        return self.search(identity, allow_approximate=allow_approximate)

    def register(self, registration: Registration, contents: dict[str, bytes]) -> AssetVersion:
        identity_key = registration.identity.key
        asset_id = digest([self.scope.key, identity_key])
        original = next(r for r in registration.representations if r.derived_from is None)
        if set(contents) != {r.name for r in registration.representations}:
            raise ValueError("Provide exactly the declared representations.")
        for rep in registration.representations:
            data = contents[rep.name]
            if len(data) != rep.size_bytes or hashlib.sha256(data).hexdigest() != rep.sha256:
                raise ValueError("Representation size or SHA-256 mismatch.")
        validation = (inspect_step(contents[original.name], registration.dimensions_mm, registration.tolerance_mm)
                      if original.format.lower() in {"step", "stp"} else
                      {"status": "unverified", "limitations": ["Original format has no installed geometry validator; use STEP for validated reuse."]})
        payload = registration.model_dump(mode="json")
        # Normalized identity and content define replay. First observation retains its provenance date.
        fingerprint = {**payload, "identity": identity_key,
                       "provenance": {k: v for k, v in payload["provenance"].items() if k != "retrieved_at"}}
        version = digest([fingerprint, validation])
        asset = AssetVersion(asset_id=asset_id, version=version, registration=registration,
                             validation=validation, created_at=datetime.now(timezone.utc).isoformat())
        for rep in registration.representations:
            # Read/verify existing content. Do not trust presence alone after an interrupted ingestion.
            try:
                existing = self.storage.get(self.blob_scope, rep.sha256, rep.media_type).content
            except (FileNotFoundError, ProjectArtifactStorageError):
                existing = None
            if existing != contents[rep.name]:
                self.storage.put(self.blob_scope, rep.sha256, contents[rep.name], rep.media_type)
                self._event("blob_writes")
        # Commit metadata only after every byte is durable. Concurrent identical ingestion is a no-op.
        saved = self.repository.insert(self.scope.key, identity_key, asset)
        self._event("registrations", asset_id=asset_id)
        return AssetVersion.model_validate(saved)

    def revalidate(self, asset_id: str, version: str) -> AssetVersion:
        asset = self.inspect(asset_id, version)
        contents = {r.name: self.content(asset_id, version, r.name) for r in asset.registration.representations}
        # A changed validator creates a new immutable version, never alters historical pins.
        return self.register(asset.registration, contents)

    def pin(self, request: AttachRequest) -> dict:
        asset = self.inspect(request.asset_id, request.version)
        if asset.validation["status"] != "validated":
            raise ValueError("Revalidate the component before attaching it.")
        if asset.registration.approximate and not request.allow_approximate:
            raise ValueError("Explicitly accept the approximate geometry and its limitations.")
        self.verify(asset)
        return {**request.model_dump(mode="json"), "identity": asset.registration.identity.model_dump(mode="json"),
                "validation": asset.validation, "limitations": asset.registration.limitations,
                "representations": [r.model_dump(mode="json") for r in asset.registration.representations]}
