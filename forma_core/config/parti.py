"""Parti model identities and capabilities for the Runpod seed adapter."""

from dataclasses import dataclass

from forma_core.config.environment import config


DEFAULT_PARTI_MODEL_ID = "caid-technologies/parti-vision"
LEGACY_PARTI_MODEL_ID = "caid-technologies/parti-base"


@dataclass(frozen=True)
class PartiModel:
    """The selected model's input contract, independent of its served name."""

    model_id: str
    supports_images: bool
    adapter_version: str = "parti-seed-v1"


def resolve_parti_model(provider_name: str, model_name: str) -> PartiModel | None:
    """Recognize current, configured, and legacy Parti models on Runpod.

    RUNPOD_PARTI_MODEL optionally names a future vision-compatible checkpoint or
    serving alias. Normal selection still uses the existing Runpod model options.
    """
    if provider_name != "runpod":
        return None
    if model_name == LEGACY_PARTI_MODEL_ID:
        return PartiModel(model_name, supports_images=False, adapter_version="parti-base-v1")
    current_model = config.optional("RUNPOD_PARTI_MODEL") or DEFAULT_PARTI_MODEL_ID
    if model_name in {DEFAULT_PARTI_MODEL_ID, current_model}:
        return PartiModel(model_name, supports_images=True)
    return None
