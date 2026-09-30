from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.objects import namespace_payload


VIDEO_PROMPT_NAMESPACES = (
    "product.overview",
    "product.electrical",
    "product.mech",
    "product.visuals",
    "product.assembly",
)
IMAGE_TO_VIDEO_PROMPT_MAX_CHARS = 2400


def _clean_text(value: Any, *, max_chars: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_chars].rstrip()


def _limit_prompt(value: str, *, max_chars: int = IMAGE_TO_VIDEO_PROMPT_MAX_CHARS) -> str:
    prompt = str(value or "").strip()
    if len(prompt) <= max_chars:
        return prompt
    return prompt[:max_chars].rstrip()


def _component_label(component: Dict[str, Any]) -> str:
    ref = _clean_text(component.get("ref_des"), max_chars=24)
    name = _clean_text(component.get("name") or component.get("part_number"), max_chars=80)
    category = _clean_text(component.get("category"), max_chars=40)
    parts = [part for part in (ref, name, category) if part]
    return " ".join(parts)


def _top_components(payload: Dict[str, Any], limit: int = 6) -> List[str]:
    bom = payload.get("bom") if isinstance(payload.get("bom"), list) else []
    components = bom or (payload.get("components") if isinstance(payload.get("components"), list) else [])
    labels = []
    for component in components:
        if not isinstance(component, dict):
            continue
        label = _component_label(component)
        if label:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _mechanical_details(payload: Dict[str, Any]) -> List[str]:
    mechanical = payload.get("mechanical") if isinstance(payload.get("mechanical"), dict) else {}
    details = []
    dimensions = (
        mechanical.get("dimensions_mm")
        or mechanical.get("dimensions")
        or mechanical.get("render_dimensions")
        or payload.get("render_dimensions")
    )
    if isinstance(dimensions, dict):
        dims = [dimensions.get(key) for key in ("x", "x_mm", "width", "y", "y_mm", "depth", "z", "z_mm", "height")]
        numeric_dims = [str(value) for value in dims if isinstance(value, (int, float))]
        if numeric_dims:
            details.append(f"compact enclosure around {' x '.join(numeric_dims[:3])} mm")
    enclosure = _clean_text(
        mechanical.get("enclosure")
        or mechanical.get("enclosure_style")
        or mechanical.get("enclosure_type")
        or mechanical.get("notes"),
        max_chars=140,
    )
    if enclosure:
        details.append(enclosure)
    mounting = _clean_text(mechanical.get("mounting_guidance"), max_chars=140)
    if mounting:
        details.append(mounting)
    fabrication_details = mechanical.get("fabrication_details") if isinstance(mechanical.get("fabrication_details"), list) else []
    for note in fabrication_details[:2]:
        cleaned = _clean_text(note, max_chars=120)
        if cleaned:
            details.append(cleaned)
    fabrication_notes = payload.get("fabrication_notes") if isinstance(payload.get("fabrication_notes"), list) else []
    for note in fabrication_notes[:2]:
        cleaned = _clean_text(note, max_chars=120)
        if cleaned:
            details.append(cleaned)
    return details[:3]


def _visual_details(payload: Dict[str, Any]) -> List[str]:
    details = []
    for key in (
        "product_image_prompt",
        "image_prompt",
        "visual_prompt",
        "product_image_model",
        "image_output_model",
    ):
        cleaned = _clean_text(payload.get(key), max_chars=180)
        if cleaned:
            details.append(cleaned)
    sequence = payload.get("product_visual_sequence")
    if isinstance(sequence, list):
        for item in sequence[:3]:
            if not isinstance(item, dict):
                continue
            label = _clean_text(item.get("label") or item.get("view_id"), max_chars=80)
            prompt = _clean_text(item.get("prompt"), max_chars=140)
            if label or prompt:
                details.append(": ".join(part for part in (label, prompt) if part))
    return details[:4]


def _assembly_details(payload: Dict[str, Any]) -> List[str]:
    steps = payload.get("assembly") if isinstance(payload.get("assembly"), list) else []
    details = []
    for step in steps[:3]:
        if not isinstance(step, dict):
            continue
        title = _clean_text(step.get("title"), max_chars=80)
        description = _clean_text(step.get("description"), max_chars=120)
        if title or description:
            details.append(": ".join(part for part in (title, description) if part))
    return details


def generate_image_to_video_prompt_from_namespaces(
    ir: HardwareIntermediateRepresentation | Dict[str, Any],
    *,
    namespaces: Optional[List[str]] = None,
) -> Dict[str, Any]:
    selected_namespaces = tuple(namespaces or VIDEO_PROMPT_NAMESPACES)
    payloads = {namespace: namespace_payload(ir, namespace) for namespace in selected_namespaces}
    overview_payload = payloads.get("product.overview", {})
    electrical_payload = payloads.get("product.electrical", {})
    mech_payload = payloads.get("product.mech", {})
    visuals_payload = payloads.get("product.visuals", {})
    assembly_payload = payloads.get("product.assembly", {})

    overview = overview_payload.get("overview") if isinstance(overview_payload.get("overview"), dict) else {}
    requirements = overview_payload.get("requirements") if isinstance(overview_payload.get("requirements"), dict) else {}
    title = _clean_text(overview.get("title") or "Generated hardware project", max_chars=100)
    description = _clean_text(overview.get("description"), max_chars=220)
    component_labels = _top_components(electrical_payload)
    mech_details = _mechanical_details(mech_payload)
    visual_details = _visual_details(visuals_payload)
    assembly_details = _assembly_details(assembly_payload)
    constraints = overview_payload.get("constraints") if isinstance(overview_payload.get("constraints"), list) else []
    power = _clean_text(requirements.get("power_needs"), max_chars=140)

    parts_line = ", ".join(component_labels) if component_labels else "the visible electronics modules"
    motion_beats = [
        "start on the generated still image and animate a slow 3/4 orbit",
        "reveal the enclosure, mounted electronics, wiring paths, display, connectors, and power source",
        "use subtle parallax, realistic lighting, and clean product-render motion",
        "keep the hardware coherent across frames with no extra invented components",
    ]
    if assembly_details:
        motion_beats.append(f"briefly imply assembly sequence details: {'; '.join(assembly_details[:2])}")

    prompt_lines = [
        f"Image-to-video prompt for {title}.",
        description,
        f"Main visible parts: {parts_line}.",
        f"Mechanical context: {'; '.join(mech_details) if mech_details else 'compact low-voltage maker hardware enclosure with clearly mounted components'}.",
        f"Visual context: {'; '.join(visual_details) if visual_details else 'clean realistic product concept render, inspection-friendly framing'}.",
        power and f"Power context: {power}.",
        constraints and f"Respect constraints: {'; '.join(_clean_text(item, max_chars=90) for item in constraints[:3])}.",
        f"Motion: {'; '.join(motion_beats)}.",
        "Avoid text glitches, impossible wiring, floating parts, extra screens, mismatched displays, or changing component count.",
        "Style: realistic prototype product video, neutral background, stable camera, no people, no marketing text overlays.",
    ]
    raw_prompt = "\n".join(line for line in prompt_lines if line)
    prompt = _limit_prompt(raw_prompt)
    return {
        "prompt": prompt,
        "prompt_max_chars": IMAGE_TO_VIDEO_PROMPT_MAX_CHARS,
        "prompt_truncated": len(prompt) < len(raw_prompt),
        "namespaces": list(selected_namespaces),
        "title": title,
        "component_count": len(component_labels),
    }


__all__ = [
    "IMAGE_TO_VIDEO_PROMPT_MAX_CHARS",
    "VIDEO_PROMPT_NAMESPACES",
    "generate_image_to_video_prompt_from_namespaces",
]
