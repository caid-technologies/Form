from __future__ import annotations

import hashlib
import re

from forma_core.agents.clarification import ask_clarifying_questions
from forma_core.workspaces.context.models import ContextAttachment, ContextGatheringRequest
from forma_core.workspaces.design_briefs import (
    DESIGN_BRIEF_SCHEMA_VERSION,
    DesignBrief,
    DesignBriefCreate,
    DesignBriefReadiness,
    DesignBriefReference,
)
from forma_core.workspaces.design_briefs.references import inline_image_metadata
from forma_core.workspaces.projects.models import ClarifyingQuestionsRequest


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _sentences(text: str) -> list[str]:
    return _unique(re.split(r"(?:[\r\n]+|(?<=[.!?])\s+)", text.strip()))


def _intent(text: str, previous: DesignBrief | None) -> str:
    lowered = text.casefold()
    if any(word in lowered for word in ("modify", "revise", "change", "iterate", "update")):
        return "modify a hardware design"
    if any(word in lowered for word in ("reverse engineer", "identify this", "recreate this")):
        return "reverse engineer a hardware reference"
    if any(word in lowered for word in ("validate", "review", "check this design")):
        return "validate a hardware design"
    if previous:
        return previous.intent
    return "design a buildable hardware product"


def _requested_outputs(text: str) -> list[str]:
    mappings = {
        "wiring": ("wire", "wiring", "pinout"),
        "schematic": ("schematic", "circuit diagram"),
        "bom": ("bom", "bill of materials", "parts list"),
        "firmware": ("firmware", "code", "software"),
        "enclosure": ("enclosure", "case", "housing"),
        "cad": ("cad", "step file", "stl", "3d model"),
        "product images": ("product image", "render", "concept image"),
        "validation": ("validation", "test plan", "verify"),
        "assembly instructions": ("assembly", "build guide", "instructions"),
    }
    lowered = text.casefold()
    return [output for output, markers in mappings.items() if any(marker in lowered for marker in markers)]


def _reference(attachment: ContextAttachment) -> DesignBriefReference:
    identity = attachment.attachment_id
    if not identity:
        digest_source = "|".join((attachment.kind, attachment.name or "", attachment.uri or "", attachment.data_url or ""))
        identity = f"attachment-{hashlib.sha256(digest_source.encode()).hexdigest()[:20]}"
    metadata: dict[str, object] = inline_image_metadata(
        attachment.data_url,
        {"source": attachment.source, **dict(attachment.metadata or {})},
    )
    if attachment.extracted_text:
        metadata["text_extracted"] = True
    return DesignBriefReference(
        reference_id=identity,
        kind=f"uploaded_{attachment.kind}",
        label=attachment.name,
        uri=attachment.uri,
        media_type=attachment.media_type,
        metadata=metadata,
    )


def _question_answered(question: str, context: str) -> bool:
    question_lower = question.casefold()
    context_lower = context.casefold()
    answer_patterns: tuple[tuple[tuple[str, ...], str], ...] = (
        (("controller", "major modules"), r"\b(esp32|arduino|raspberry|stm32|rp2040|controller|module)\b"),
        (
            ("overall shape", "silhouette", "form factor"),
            r"\b(rectangular|square|box|round|rounded|circular|cylindrical|cylinder|radial|curved|handheld|wearable|folded|open[-\s]?frame|exposed|puck|pod|tower|wall[-\s]?mounted)\b",
        ),
        (("power", "battery", "adapter", "rail"), r"(?:\b\d+(?:\.\d+)?\s*v\b|\busb(?:-c)?\b|\bbattery\b|\badapter\b|\bno mains\b)"),
        (("control or display", "system control", "outputs"), r"\b(display|oled|screen|relay|motor|pump|fan|led|buzzer|actuator|log|control)\b"),
        (("weather", "environment", "where will"), r"\b(indoor|outdoor|field|bench|lab|rain|wind|weather|temperature)\b"),
        (("successful", "success", "validated"), r"\b(success|validate|validation|test|verify|within|under\s+\d+)\b"),
        (("hard constraints", "constraints should"), r"\b(must|only|under|within|budget|voltage|usb|battery|waterproof|weatherproof|no\s+)\b"),
        (("optimize", "artifacts"), r"\b(wiring|schematic|bom|bill of materials|firmware|cad|enclosure|image|render|validation|assembly)\b"),
        (("who uses", "where does", "use case"), r"\b(user|operator|student|engineer|indoor|outdoor|field|bench|lab|classroom|consumer)\b"),
    )
    for question_markers, answer_pattern in answer_patterns:
        if any(marker in question_lower for marker in question_markers):
            return bool(re.search(answer_pattern, context_lower, re.IGNORECASE))
    return False


def _expresses_uncertainty(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return bool(re.search(
        r"^(?:idk|i do not know|i don t know|don t know|not sure|unsure|no idea|you choose|up to you)$",
        normalized,
    ))


def _asks_for_explanation(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    return bool(re.search(
        r"\b(?:what do you mean|what does .+ mean|can you explain|could you explain|why do you ask|why are you asking)\b",
        normalized,
    ))


def _explain_pending_question(question: str) -> str:
    normalized = question.casefold()
    if "treated as fixed" in normalized or ("controller" in normalized and "module" in normalized):
        return (
            "By “fixed,” I mean a controller or module that is already decided because you own it, must reuse it, or need compatibility with it. "
            "If nothing is predetermined, say “no preference,” and the build agents can choose compatible parts from the rest of your requirements."
        )
    return (
        f"I’m asking about this open design choice: “{question}” "
        "Share only what is already decided. If nothing is decided, say “no preference,” and the build agents can propose it from your goals and constraints."
    )


def _assistant_reply(text: str, questions: list[str], previous: DesignBrief | None) -> str:
    if _asks_for_explanation(text) and previous and previous.unresolved_questions:
        return _explain_pending_question(previous.unresolved_questions[0])
    if _expresses_uncertainty(text):
        return (
            "That’s okay—you don’t need to choose technical parts yet. "
            "Tell me any outcome, operating condition, or constraint you do know, and the build agents can propose the open technical choices. "
            "Choose Default whenever you’re ready, and the build agents will resolve the remaining technical choices."
        )
    if previous:
        if questions:
            return (
                "Thanks—that helps shape the brief. Add any behavior, operating condition, constraint, or preference that matters to you; "
                "technical choices can stay open for the build agents."
            )
        return "Thanks—that gives the build agents enough direction. Add anything else that matters, or continue to the next stage."
    if questions:
        return (
            f"Got it—I’ve started the design brief. {questions[0]} "
            "If you don’t know the technical details, describe the outcome you want and the build agents can propose the implementation."
        )
    return "Got it—I’ve started the design brief. Add anything else that matters, or continue to the next stage."


class ContextBriefUpdater:
    """Updates a DesignBrief without invoking a model, tool, or worker job."""

    def update(
        self,
        request: ContextGatheringRequest,
        previous: DesignBrief | None = None,
    ) -> tuple[DesignBriefCreate, str, list[str], list[str]]:
        text = request.text.strip()
        attachment_text = [item.extracted_text for item in request.attachments if item.extracted_text]
        combined_update = "\n".join([text, *attachment_text]).strip()
        conversational_control = _asks_for_explanation(text) or _expresses_uncertainty(text)
        update_sentences = [] if conversational_control else _sentences(combined_update)

        prior_requirements = [
            requirement
            for requirement in (list(previous.requirements) if previous else [])
            if not _asks_for_explanation(requirement) and not _expresses_uncertainty(requirement)
        ]
        requirements = _unique([*prior_requirements, *update_sentences])
        constraint_markers = re.compile(
            r"\b(must|only|under|maximum|max\b|minimum|min\b|budget|fit|within|voltage|battery|usb|no\s+|without|weatherproof|waterproof|material|shape|silhouette|form factor|rectangular|square|round|rounded|circular|cylindrical|radial|curved|handheld|wearable|folded|open[-\s]?frame|puck|pod)\b",
            re.IGNORECASE,
        )
        constraints = _unique([
            *(list(previous.constraints) if previous else []),
            *(sentence for sentence in update_sentences if constraint_markers.search(sentence)),
        ])
        assumption_markers = re.compile(r"\b(assume|assuming|probably|for now)\b", re.IGNORECASE)
        assumptions = _unique([
            *(list(previous.assumptions) if previous else []),
            *(sentence for sentence in update_sentences if assumption_markers.search(sentence)),
        ])
        validation_markers = re.compile(r"\b(success|validate|validation|test|verify|passes|works when)\b", re.IGNORECASE)
        validation = _unique([
            *(list(previous.validation_criteria) if previous else []),
            *(sentence for sentence in update_sentences if validation_markers.search(sentence)),
        ])

        references = list(previous.references) if previous else []
        references_by_id = {item.reference_id: item for item in references}
        for attachment in request.attachments:
            item = _reference(attachment)
            references_by_id[item.reference_id] = item

        prior_outputs = list(previous.requested_outputs) if previous else []
        requested_outputs = _unique([*prior_outputs, *_requested_outputs(combined_update)])
        full_context = "\n".join(_unique([*requirements, *constraints]))
        clarification = ask_clarifying_questions(ClarifyingQuestionsRequest(
            prompt=full_context,
            has_image=any(item.kind == "image" for item in request.attachments) or any(
                reference.kind == "uploaded_image" for reference in references_by_id.values()
            ),
            workflow="default",
            force=False,
            max_questions=3,
        ))
        prior_questions = [
            question
            for question in (list(previous.unresolved_questions) if previous else [])
            if not _question_answered(question, combined_update)
        ]
        new_questions = [
            question.question
            for question in clarification.questions
            if not _question_answered(question.question, full_context)
        ]
        questions = _unique([*prior_questions, *new_questions])
        suggestions = next((
            list(question.suggestions)
            for question in clarification.questions
            if question.question in questions and question.suggestions
        ), [])

        if previous and previous.summary:
            summary = previous.summary
        elif text:
            summary = text[:500]
        elif request.attachments:
            kinds = ", ".join(sorted({item.kind for item in request.attachments}))
            summary = f"Hardware design based on the supplied {kinds} reference."
        else:  # model validation makes this unreachable
            summary = "Hardware design context"

        brief = DesignBriefCreate(
            schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
            conversation_id=request.conversation_id,
            intent=_intent(combined_update, previous),
            summary=summary,
            requirements=requirements,
            constraints=constraints,
            references=list(references_by_id.values()),
            requested_outputs=requested_outputs,
            validation_criteria=validation,
            unresolved_questions=questions,
            assumptions=assumptions,
            readiness=DesignBriefReadiness.NEEDS_CLARIFICATION if questions else DesignBriefReadiness.DRAFT,
        )
        assistant_message = _assistant_reply(text, questions, previous)
        return brief, assistant_message, questions, suggestions
