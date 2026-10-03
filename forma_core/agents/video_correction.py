from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from forma_core.video_review import (
    DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL,
    FireworksVideoReviewClient,
    VideoIterationReview,
    VideoReviewClient,
)
from forma_core.workspaces.projects.iteration import ProjectIterator, coerce_hardware_ir
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.objects import normalize_project_namespace


logger = logging.getLogger(__name__)


class FireworksVideoSelfCorrectionAgent:
    """Review a generated video, then apply the review as a project iteration."""

    def __init__(
        self,
        *,
        review_client: Optional[VideoReviewClient] = None,
        iterator: Optional[ProjectIterator] = None,
        **iterator_kwargs: Any,
    ) -> None:
        self.review_client = review_client or FireworksVideoReviewClient()
        self.iterator = iterator or ProjectIterator(**iterator_kwargs)

    def get_debug_config(self) -> Dict[str, Any]:
        return {
            "operation": "video_self_correction",
            "review": self.review_client.get_debug_config() if hasattr(self.review_client, "get_debug_config") else {},
            "iteration": self.iterator.get_debug_config(),
        }

    def review_video(
        self,
        current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
        *,
        video_url: str,
        original_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> VideoIterationReview:
        ir = coerce_hardware_ir(current_ir)
        return self.review_client.review_video(
            ir,
            video_url=video_url,
            original_prompt=original_prompt,
            project_id=project_id,
        )

    def correct_project_from_video(
        self,
        current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
        *,
        video_url: str,
        original_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
        target_namespace: Optional[str] = None,
    ) -> tuple[HardwareIntermediateRepresentation, VideoIterationReview]:
        ir = coerce_hardware_ir(current_ir)
        logger.info(
            "Starting video self-correction iteration: project_id=%s review_model=%s target_namespace=%s",
            project_id or (ir.assembly_metadata or {}).get("project_id") or "unknown",
            getattr(self.review_client, "model", DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL),
            normalize_project_namespace(target_namespace) or "auto",
        )
        review = self.review_video(ir, video_url=video_url, original_prompt=original_prompt, project_id=project_id)
        namespace = normalize_project_namespace(target_namespace) or review.target_namespace
        logger.info(
            "Applying video self-correction iteration: project_id=%s target_namespace=%s coherence_score=%.3f issue_count=%s",
            project_id or (ir.assembly_metadata or {}).get("project_id") or "unknown",
            namespace,
            review.coherence_score,
            len(review.issues),
        )
        revised = self.iterator.iterate_project(
            ir,
            review.iteration_instruction,
            original_prompt=original_prompt,
            project_id=project_id,
            target_namespace=namespace,
        )
        metadata = dict(revised.assembly_metadata or {})
        review_payload = {
            **review.model_dump(mode="json"),
            "video_url": video_url,
            "review_provider": "fireworks",
            "review_model": getattr(self.review_client, "model", DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL),
        }
        metadata["video_self_correction"] = review_payload
        last_iteration = dict(metadata.get("last_iteration") or {})
        last_iteration["video_review"] = {
            "summary": review.summary,
            "coherence_score": review.coherence_score,
            "issue_count": len(review.issues),
            "review_model": review_payload["review_model"],
        }
        metadata["last_iteration"] = last_iteration
        revised.assembly_metadata = metadata
        if revised.project_version_history:
            revised.project_version_history[-1] = {
                **dict(revised.project_version_history[-1]),
                "video_review": last_iteration["video_review"],
            }
        logger.info(
            "Completed video self-correction iteration: project_id=%s revision=%s target_namespace=%s",
            project_id or metadata.get("project_id") or "unknown",
            metadata.get("revision"),
            metadata.get("iteration_target_namespace") or namespace,
        )
        return revised, review


__all__ = ["FireworksVideoSelfCorrectionAgent"]
