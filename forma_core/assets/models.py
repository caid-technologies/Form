"""Library identities are explicit; unknown revisions are never wildcards."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class AssetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class AssetScope(AssetModel):
    owner_user_id: str = Field(min_length=1)
    workspace_id: str = ""

    @property
    def key(self) -> str:
        # Private even inside a workspace. No inferred permission to share uploads.
        return digest(self.model_dump())


class ComponentIdentity(AssetModel):
    manufacturer: str = Field(min_length=1, max_length=200)
    part_number: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)
    variants: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        variants = {normalized(k): normalized(v) for k, v in self.variants.items()}
        if len(variants) != len(self.variants) or any(not k or not v for k, v in variants.items()):
            raise ValueError("Variant keys must be distinct and values must be explicit.")
        return digest({"manufacturer": normalized(self.manufacturer), "part_number": normalized(self.part_number),
                       "revision": normalized(self.revision), "variants": variants})


    @property
    def family_key(self) -> str:
        return digest([normalized(self.manufacturer), normalized(self.revision),
                       {normalized(k): normalized(v) for k, v in self.variants.items()}])


class Provenance(AssetModel):
    origin: Literal["sourced", "uploaded", "generated"]
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    product_url: str = ""
    retrieved_at: datetime
    license: str = Field(min_length=1, max_length=2000)
    usage_notes: str = Field(min_length=1, max_length=4000)

    @field_validator("source_urls")
    @classmethod
    def urls_are_evidence_only(cls, values: list[str]) -> list[str]:
        if any(not value.startswith(("https://", "http://")) for value in values):
            raise ValueError("Use HTTP(S) provenance URLs; the library does not fetch URLs.")
        return values


class Representation(AssetModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    format: str = Field(min_length=1, max_length=40)
    media_type: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    derived_from: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    conversion: dict[str, JsonValue] = Field(default_factory=dict)


class Registration(AssetModel):
    identity: ComponentIdentity
    aliases: list[str] = Field(default_factory=list, max_length=40)
    provenance: Provenance
    units: Literal["mm"] = "mm"
    dimensions_mm: tuple[float, float, float] | None = None
    tolerance_mm: float = Field(default=0.5, ge=0, le=5)
    approximate: bool = False
    limitations: list[str] = Field(default_factory=list, max_length=40)
    representations: list[Representation] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def check_representations(self):
        originals = [r for r in self.representations if r.derived_from is None]
        if len(originals) != 1:
            raise ValueError("Exactly one original representation is required.")
        if len({r.name for r in self.representations}) != len(self.representations):
            raise ValueError("Representation names must be unique.")
        hashes = {r.sha256 for r in self.representations}
        if any(r.derived_from and (r.derived_from not in hashes or r.derived_from == r.sha256 or not r.conversion)
               for r in self.representations):
            raise ValueError("Derived assets need a different, included source hash and conversion settings.")
        parents = {r.sha256: r.derived_from for r in self.representations}
        for representation in self.representations:
            seen = set()
            current = representation.sha256
            while parents[current] is not None:
                if current in seen:
                    raise ValueError("Derivation links must be acyclic and lead to the original.")
                seen.add(current)
                current = parents[current]
        if self.dimensions_mm and any(v <= 0 for v in self.dimensions_mm):
            raise ValueError("Dimensions must be positive.")
        if self.approximate and not self.limitations:
            raise ValueError("Approximate geometry requires explicit limitations.")
        return self


class AssetVersion(AssetModel):
    asset_id: str
    version: str
    registration: Registration
    validation: dict[str, JsonValue]
    created_at: str


class SearchRequest(AssetModel):
    identity: ComponentIdentity
    allow_approximate: bool = False


class InspectRequest(AssetModel):
    asset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str = Field(pattern=r"^[a-f0-9]{64}$")


class AttachRequest(InspectRequest):
    instance_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    position_mm: tuple[float, float, float] = (0, 0, 0)
    rotation_deg: tuple[float, float, float] = (0, 0, 0)
    allow_approximate: bool = False


class RegisterRequest(AssetModel):
    registration: Registration
    # A bounded transfer boundary, never a worker filesystem path or fetched URL.
    content_base64: dict[str, str]


class AssetToolArguments(AssetModel):
    request_json: str = Field(min_length=2, max_length=8 * 1024 * 1024)
