"""Shared package, hosted protocol, and Hardware Intermediate Representation compatibility policy."""

from __future__ import annotations

from enum import StrEnum
import re
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from forma_core._version import __version__
from forma_core.config.environment import config


CURRENT_PROTOCOL_VERSION = 1
CURRENT_HARDWARE_IR_VERSION = "0.2"
SUPPORTED_HARDWARE_IR_VERSIONS = frozenset({CURRENT_HARDWARE_IR_VERSION})
UPGRADE_COMMAND = "pipx upgrade forma-oss"
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class CompatibilityStatus(StrEnum):
    CURRENT = "CURRENT"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    UNSUPPORTED_CLIENT = "UNSUPPORTED_CLIENT"
    UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
    UNSUPPORTED_HARDWARE_IR = "UNSUPPORTED_HARDWARE_IR"
    REMOTE_VERSION_UNAVAILABLE = "REMOTE_VERSION_UNAVAILABLE"


class CompatibilityMetadata(BaseModel):
    """Machine-readable compatibility information published by hosted Forma."""

    model_config = ConfigDict(extra="ignore")

    latest_version: str
    minimum_supported_version: str
    protocol_version: int = Field(ge=1)
    hardware_ir_version: str
    supported_hardware_ir_versions: list[str] = Field(default_factory=list)
    upgrade_message: str | None = None


class CompatibilityResult(BaseModel):
    """The result of comparing one client with a hosted compatibility contract."""

    model_config = ConfigDict(extra="ignore")

    status: CompatibilityStatus
    client_version: str
    latest_version: str | None = None
    minimum_supported_version: str | None = None
    client_protocol_version: int | None = None
    protocol_version: int | None = None
    hardware_ir_version: str | None = None
    message: str
    upgrade_command: str = UPGRADE_COMMAND

    @property
    def is_blocking(self) -> bool:
        return self.status in {
            CompatibilityStatus.UNSUPPORTED_CLIENT,
            CompatibilityStatus.UNSUPPORTED_PROTOCOL,
            CompatibilityStatus.UNSUPPORTED_HARDWARE_IR,
        }


class UnsupportedHardwareIntermediateRepresentationVersion(ValueError):
    """Raised when a serialized Hardware Intermediate Representation document uses an unsupported schema."""

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"Hardware Intermediate Representation schema version {version!r} is not supported by this Forma service.")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"Invalid Forma version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def compare_versions(left: str, right: str) -> int:
    """Compare two release versions without adding a packaging dependency."""

    left_tuple = _version_tuple(left)
    right_tuple = _version_tuple(right)
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


def _configured_version(*names: str, default: str) -> str:
    for name in names:
        value = config.optional(name)
        if value and value.strip():
            return value.strip()
    return default


def _configured_protocol_version() -> int:
    raw = config.optional("FORMA_HOSTED_PROTOCOL_VERSION")
    if not raw:
        return CURRENT_PROTOCOL_VERSION
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("FORMA_HOSTED_PROTOCOL_VERSION must be an integer.") from exc
    if value < 1:
        raise ValueError("FORMA_HOSTED_PROTOCOL_VERSION must be at least 1.")
    return value


def hosted_compatibility_metadata() -> CompatibilityMetadata:
    """Resolve the hosted policy from explicit deployment configuration."""

    latest = _configured_version(
        "FORMA_HOSTED_LATEST_VERSION",
        "FORMA_LATEST_VERSION",
        default=__version__,
    )
    minimum = _configured_version(
        "FORMA_HOSTED_MINIMUM_SUPPORTED_VERSION",
        "FORMA_MINIMUM_SUPPORTED_VERSION",
        default=__version__,
    )
    if compare_versions(minimum, latest) > 0:
        raise ValueError("The minimum supported Forma version cannot be newer than the latest version.")
    supported = sorted(
        {
            item.strip()
            for item in (config.optional("FORMA_SUPPORTED_HARDWARE_IR_VERSIONS") or CURRENT_HARDWARE_IR_VERSION).split(",")
            if item.strip()
        }
    )
    if not supported:
        supported = [CURRENT_HARDWARE_IR_VERSION]
    current_ir = config.optional("FORMA_HARDWARE_IR_VERSION") or CURRENT_HARDWARE_IR_VERSION
    if current_ir not in supported:
        supported.insert(0, current_ir)
    return CompatibilityMetadata(
        latest_version=latest,
        minimum_supported_version=minimum,
        protocol_version=_configured_protocol_version(),
        hardware_ir_version=current_ir,
        supported_hardware_ir_versions=supported,
        upgrade_message=config.optional("FORMA_UPGRADE_MESSAGE"),
    )


def evaluate_compatibility(
    client_version: str,
    metadata: CompatibilityMetadata,
    *,
    client_protocol_version: int = CURRENT_PROTOCOL_VERSION,
    hardware_ir_version: str | None = None,
) -> CompatibilityResult:
    """Compare client dimensions in order of the most actionable failure."""

    base = {
        "client_version": client_version,
        "latest_version": metadata.latest_version,
        "minimum_supported_version": metadata.minimum_supported_version,
        "client_protocol_version": client_protocol_version,
        "protocol_version": metadata.protocol_version,
        "hardware_ir_version": hardware_ir_version,
    }
    if client_protocol_version != metadata.protocol_version:
        return CompatibilityResult(
            **base,
            status=CompatibilityStatus.UNSUPPORTED_PROTOCOL,
            message=(
                f"Forma hosted protocol version {metadata.protocol_version} is required; "
                f"this client uses protocol version {client_protocol_version}."
            ),
        )
    if hardware_ir_version is not None and hardware_ir_version not in set(
        metadata.supported_hardware_ir_versions or [metadata.hardware_ir_version]
    ):
        return CompatibilityResult(
            **base,
            status=CompatibilityStatus.UNSUPPORTED_HARDWARE_IR,
            message=(
                f"Hardware Intermediate Representation schema version {hardware_ir_version} is not supported by the Forma hosted API. "
                f"Supported version(s): {', '.join(metadata.supported_hardware_ir_versions or [metadata.hardware_ir_version])}."
            ),
        )
    if compare_versions(client_version, metadata.minimum_supported_version) < 0:
        return CompatibilityResult(
            **base,
            status=CompatibilityStatus.UNSUPPORTED_CLIENT,
            message=(
                f"Forma-OSS {client_version} is no longer compatible with the Forma hosted API. "
                f"Minimum supported version: {metadata.minimum_supported_version}."
            ),
        )
    if compare_versions(client_version, metadata.latest_version) < 0:
        return CompatibilityResult(
            **base,
            status=CompatibilityStatus.UPDATE_AVAILABLE,
            message=metadata.upgrade_message
            or (
                f"Forma-OSS {metadata.latest_version} is available. You are running {client_version}. "
                f"Upgrade with `{UPGRADE_COMMAND}`."
            ),
        )
    return CompatibilityResult(
        **base,
        status=CompatibilityStatus.CURRENT,
        message=f"Forma-OSS {client_version} is compatible with the hosted API.",
    )


def hardware_ir_version_from_document(document: Mapping[str, Any]) -> str | None:
    """Read an explicitly serialized IR version before Pydantic migrations normalize it."""

    nested = document.get("project_ir")
    if not isinstance(nested, Mapping):
        nested = document.get("hardware_ir")
    if not isinstance(nested, Mapping):
        nested = document
    value = nested.get("hardware_ir_version")
    return str(value).strip() if value is not None and str(value).strip() else None


def ensure_supported_hardware_ir_version(
    document: Mapping[str, Any],
    *,
    supported_versions: set[str] | frozenset[str] | list[str] | None = None,
) -> None:
    version = hardware_ir_version_from_document(document)
    supported = set(supported_versions or SUPPORTED_HARDWARE_IR_VERSIONS)
    if version is not None and version not in supported:
        raise UnsupportedHardwareIntermediateRepresentationVersion(version)


def unavailable_result(client_version: str) -> CompatibilityResult:
    return CompatibilityResult(
        status=CompatibilityStatus.REMOTE_VERSION_UNAVAILABLE,
        client_version=client_version,
        message="Hosted Forma compatibility information is unavailable; continuing without a remote version check.",
    )


def compatibility_error_detail(result: CompatibilityResult) -> dict[str, Any]:
    """Return a stable server/client error payload for blocked operations."""

    code_by_status = {
        CompatibilityStatus.UNSUPPORTED_CLIENT: "UNSUPPORTED_CLIENT_VERSION",
        CompatibilityStatus.UNSUPPORTED_PROTOCOL: "UNSUPPORTED_PROTOCOL_VERSION",
        CompatibilityStatus.UNSUPPORTED_HARDWARE_IR: "UNSUPPORTED_HARDWARE_IR_VERSION",
    }
    return {
        "code": code_by_status[result.status],
        "message": result.message,
        "latest_version": result.latest_version,
        "minimum_supported_version": result.minimum_supported_version,
        "protocol_version": result.protocol_version,
        "hardware_ir_version": result.hardware_ir_version,
        "upgrade_command": result.upgrade_command,
    }


__all__ = [
    "CURRENT_HARDWARE_IR_VERSION",
    "CURRENT_PROTOCOL_VERSION",
    "CompatibilityMetadata",
    "CompatibilityResult",
    "CompatibilityStatus",
    "SUPPORTED_HARDWARE_IR_VERSIONS",
    "UPGRADE_COMMAND",
    "UnsupportedHardwareIntermediateRepresentationVersion",
    "compatibility_error_detail",
    "compare_versions",
    "ensure_supported_hardware_ir_version",
    "evaluate_compatibility",
    "hardware_ir_version_from_document",
    "hosted_compatibility_metadata",
    "unavailable_result",
]
