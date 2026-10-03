from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apps.api import main
from apps.api.auth import UserContext


ANONYMOUS_USER = UserContext(
    provider="clerk",
    subject=None,
    owner_user_id=None,
    is_authenticated=False,
    is_admin=False,
    claims={},
)


class ProjectSummaryTests(unittest.TestCase):
    def test_project_summary_can_skip_storage_hydration(self) -> None:
        project = SimpleNamespace(
            project_id="fd54de37-2fbb-485a-92e4-8bfaf4a2f08c",
            chat_id="chat_123",
            title="Low Voltage Desk Lamp",
            prompt="desk lamp",
            created_at="2026-07-21T14:08:00Z",
            owner_user_id="user_123",
            hardware_ir={
                "components": [],
                "assembly_metadata": {
                    "product_image_url": "https://storage.example.test/product.png",
                    "product_image_data": "inline-image-data",
                },
            },
        )

        with patch.object(main, "creator_display_name", return_value="isayahc"), patch.object(
            main, "hydrate_image_storage_metadata"
        ) as hydrate:
            summary = main._project_summary_response(project, hydrate_storage=False)

        hydrate.assert_not_called()
        self.assertEqual("https://storage.example.test/product.png", summary["product_image_url"])
        self.assertEqual("inline-image-data", summary["product_image_data"])

    def test_project_summary_promotes_visual_sequence_data_for_gallery_cards(self) -> None:
        project = SimpleNamespace(
            project_id="fd54de37-2fbb-485a-92e4-8bfaf4a2f08c",
            chat_id="chat_123",
            title="Low Voltage Desk Lamp",
            prompt="desk lamp",
            created_at="2026-07-21T14:08:00Z",
            owner_user_id="user_123",
            hardware_ir={
                "components": [],
                "assembly_metadata": {
                    "product_visual_sequence": [
                        {"view_id": "case", "data": "inline-image-data"},
                    ],
                },
            },
        )

        with patch.object(main, "creator_display_name", return_value="isayahc"), patch.object(
            main, "hydrate_image_storage_metadata"
        ) as hydrate:
            summary = main._project_summary_response(project, hydrate_storage=False)

        hydrate.assert_not_called()
        self.assertTrue(summary["has_product_image"])
        self.assertEqual("inline-image-data", summary["product_image_data"])

    def test_project_summary_includes_hydrated_product_image(self) -> None:
        project = SimpleNamespace(
            project_id="fd54de37-2fbb-485a-92e4-8bfaf4a2f08c",
            chat_id="chat_123",
            title="Low Voltage Desk Lamp",
            prompt="desk lamp",
            created_at="2026-07-21T14:08:00Z",
            owner_user_id="user_123",
            hardware_ir={
                "components": [{"ref_des": "D1"}, {"ref_des": "R1"}],
                "assembly_metadata": {
                    "image_output_status": "succeeded",
                    "product_image_model": "openai/gpt-image-2",
                    "product_case_image_s3_key": "images/project/product-case.png",
                    "product_visual_sequence": [
                        {
                            "view_id": "case",
                            "label": "Case exterior",
                            "s3_key": "images/project/product-case.png",
                        }
                    ],
                },
            },
        )
        hydrated = {
            **project.hardware_ir["assembly_metadata"],
            "product_case_image_url": "https://storage.example.test/signed-product-case.png",
            "product_case_image_content_type": "image/png",
            "product_visual_sequence": [
                {
                    "view_id": "case",
                    "label": "Case exterior",
                    "url": "https://storage.example.test/signed-product-case.png",
                    "content_type": "image/png",
                }
            ],
        }

        with patch.object(main, "creator_display_name", return_value="isayahc"), patch.object(
            main, "hydrate_image_storage_metadata", return_value=hydrated
        ):
            summary = main._project_summary_response(project, current_user_id="user_123")

        self.assertTrue(summary["has_product_image"])
        self.assertEqual("https://storage.example.test/signed-product-case.png", summary["product_image_url"])
        self.assertEqual("image/png", summary["product_image_content_type"])
        self.assertEqual("openai/gpt-image-2", summary["product_image_model"])
        self.assertEqual("succeeded", summary["image_output_status"])
        self.assertEqual("https://storage.example.test/signed-product-case.png", summary["product_visual_sequence"][0]["url"])

    def test_project_image_summary_endpoint_does_not_validate_full_ir(self) -> None:
        project = SimpleNamespace(
            project_id="fd54de37-2fbb-485a-92e4-8bfaf4a2f08c",
            chat_id="chat_123",
            title="Low Voltage Desk Lamp",
            prompt="desk lamp",
            created_at="2026-07-21T14:08:00Z",
            owner_user_id="user_123",
            hardware_ir={
                "components": [{"legacy_shape": "not a HardwareIntermediateRepresentation component"}],
                "assembly_metadata": {
                    "image_output_status": "succeeded",
                    "product_image_url": "https://storage.example.test/product.png",
                    "product_image_content_type": "image/png",
                },
            },
        )

        with patch.object(
            main,
            "resolve_project_for_read",
            return_value=SimpleNamespace(project=project, can_chat=False, chat_id=None),
        ), patch.object(
            main, "creator_display_name", return_value="isayahc"
        ), patch.object(
            main, "hydrate_image_storage_metadata", side_effect=lambda metadata, _project_id: metadata
        ), patch.object(
            main, "project_engagement_for_ids", return_value={}
        ), patch.object(main, "HardwareIntermediateRepresentation", side_effect=AssertionError("HardwareIntermediateRepresentation should not be constructed")):
            summary = main.get_project_image_summary_endpoint(project.project_id, ANONYMOUS_USER)

        self.assertEqual(project.project_id, summary["project_id"])
        self.assertEqual("https://storage.example.test/product.png", summary["product_image_url"])
        self.assertTrue(summary["has_product_image"])

    def test_cli_project_image_summary_does_not_enable_chat(self) -> None:
        project = SimpleNamespace(
            project_id="fd54de37-2fbb-485a-92e4-8bfaf4a2f08c",
            chat_id=None,
            title="CLI project",
            prompt="Build a CLI project.",
            created_at="2026-07-21T14:08:00Z",
            owner_user_id="user_123",
            creation_channel="cli",
            visibility="private",
            hardware_ir={"components": [], "assembly_metadata": {}},
        )

        with patch.object(
            main,
            "resolve_project_for_read",
            return_value=SimpleNamespace(project=project, can_chat=False, chat_id=None),
        ), patch.object(main, "creator_display_name", return_value="isayahc"), patch.object(
            main, "project_engagement_for_ids", return_value={}
        ):
            summary = main.get_project_image_summary_endpoint(
                project.project_id,
                UserContext(
                    provider="clerk",
                    subject="user_123",
                    owner_user_id="user_123",
                    is_authenticated=True,
                    is_admin=False,
                    claims={},
                ),
            )

        self.assertFalse(summary["can_chat"])
        self.assertIsNone(summary["chat_id"])

    def test_project_image_summary_uses_resolver_image_metadata(self) -> None:
        project = SimpleNamespace(
            project_id="canonical-only-project",
            chat_id=None,
            title="Canonical project",
            prompt="Build a canonical project.",
            created_at="2026-08-01T12:00:00Z",
            owner_user_id="user_123",
            hardware_ir={"components": [], "assembly_metadata": {}},
        )

        with patch.object(
            main,
            "resolve_project_for_read",
            return_value=SimpleNamespace(
                project=project,
                image_metadata={"product_image_url": "https://storage.example.test/canonical.png"},
                can_chat=False,
                chat_id=None,
            ),
        ), patch.object(main, "creator_display_name", return_value="isayahc"), patch.object(
            main, "project_engagement_for_ids", return_value={}
        ):
            summary = main.get_project_image_summary_endpoint(project.project_id, ANONYMOUS_USER)

        self.assertTrue(summary["has_product_image"])
        self.assertEqual("https://storage.example.test/canonical.png", summary["product_image_url"])

    def test_project_image_summary_reports_missing_storage_without_404(self) -> None:
        project = SimpleNamespace(
            project_id="canonical-missing-image",
            chat_id=None,
            title="Canonical project without image",
            prompt="Build a canonical project.",
            created_at="2026-08-01T12:00:00Z",
            owner_user_id="user_123",
            hardware_ir={"components": [], "assembly_metadata": {}},
        )

        with patch.object(
            main,
            "resolve_project_for_read",
            return_value=SimpleNamespace(
                project=project,
                image_metadata={"product_image_s3_key": "images/missing/product.png"},
                can_chat=False,
                chat_id=None,
            ),
        ), patch.object(main, "hydrate_image_storage_metadata", return_value={}), patch.object(
            main, "creator_display_name", return_value="isayahc"
        ), patch.object(main, "project_engagement_for_ids", return_value={}):
            summary = main.get_project_image_summary_endpoint(project.project_id, ANONYMOUS_USER)

        self.assertFalse(summary["has_product_image"])
        self.assertIsNone(summary["product_image_url"])
        self.assertIsNone(summary["product_image_data"])


if __name__ == "__main__":
    unittest.main()
