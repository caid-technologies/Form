from __future__ import annotations

import io
import urllib.error
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from forma_core.database import _hardware_ir_with_project_id
from forma_core.agents.project_correction import ProjectSelfCorrectionAgent
from forma_core.agents.video_correction import FireworksVideoSelfCorrectionAgent
from forma_core.workspaces.projects.iteration import ProjectIterator, compact_hardware_ir_for_iteration
from forma_core.llm import (
    LLMProviderConfigError,
    LLMProviderOutputError,
    LLMProviderValidation,
    LLMRuntimeConfig,
)
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    FunctionalRequirements,
    HardwareIntermediateRepresentation,
    PinDefinition,
    PinReference,
    ProjectOverview,
    VideoSelfCorrectRequest,
)
from forma_core.workspaces.projects.objects import (
    build_project_object,
    namespace_payload,
    normalize_project_namespace,
)
from forma_core.video_review import (
    DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL_SLUG,
    DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG,
    FIREWORKS_VIDEO_REVIEW_FRAME_MODELS,
    FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS,
    FireworksPreparedVideo,
    FireworksVideoReviewClient,
    VideoCoherenceIssue,
    VideoIterationReview,
    _distill_unstructured_review_text,
    _fireworks_http_error_message,
    _message_content_text,
    _unstructured_review_target_namespace,
)


PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def build_sample_ir() -> HardwareIntermediateRepresentation:
    component = ComponentInstance(
        ref_des="U1",
        part_number="ESP32-WROOM-32D",
        name="ESP32 Dev Board",
        category="Microcontroller",
        rationale="Runs sensing and display logic.",
        pins=[
            PinDefinition(pin_id="3V3", name="3.3V", pin_type="Power", voltage=3.3),
            PinDefinition(pin_id="GND", name="Ground", pin_type="Ground", voltage=0.0),
        ],
    )
    return HardwareIntermediateRepresentation(
        overview=ProjectOverview(
            title="Soil Monitor",
            description="A low-voltage soil moisture monitor.",
            difficulty="Beginner",
            estimated_cost=12.0,
            category="IoT",
        ),
        requirements=FunctionalRequirements(
            requirements=["Measure soil moisture"],
            power_needs="USB 5V to 3.3V regulator",
            operating_voltage=3.3,
        ),
        components=[component],
        nets=[
            ConnectionNet(
                net_id="NET_3V3",
                name="3.3V Rail",
                net_type="Power",
                voltage=3.3,
                pins=[PinReference(ref_des="U1", pin_id="3V3")],
            ),
            ConnectionNet(
                net_id="NET_GND",
                name="Ground",
                net_type="Ground",
                voltage=0.0,
                pins=[PinReference(ref_des="U1", pin_id="GND")],
            ),
        ],
        assembly_metadata={
            "project_id": PROJECT_ID,
            "revision": 1,
        },
        project_version_history=[{"version": "0.1", "description": "Initial design"}],
    )


class FakeProvider:
    provider_name = "openai"
    requested_model = "gpt-5.5"
    model_name = "gpt-5.5"
    is_configured = True

    def __init__(self, revised_ir: HardwareIntermediateRepresentation) -> None:
        self.revised_ir = revised_ir
        self.prompt = ""

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        return LLMProviderValidation(
            provider=self.provider_name,
            requested_model=self.requested_model,
            actual_model=self.model_name,
            requested_model_available=True,
            strict_mode=True,
            fallback_active=False,
            live_generation_enabled=True,
        )

    def generate_structured(self, prompt, schema_class, image_bytes=None, image_mime_type=None):
        self.prompt = prompt
        return self.revised_ir


class UnconfiguredProvider:
    provider_name = "openai"
    requested_model = "missing-model"
    model_name = "missing-model"
    is_configured = False

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        return LLMProviderValidation(
            provider=self.provider_name,
            requested_model=self.requested_model,
            actual_model=None,
            requested_model_available=False,
            strict_mode=True,
            fallback_active=False,
            live_generation_enabled=False,
            validation_error="provider is unavailable",
        )


class FakeVideoReviewClient:
    model = "fake-fireworks-vlm"

    def __init__(self) -> None:
        self.video_url = ""

    def review_video(self, current_ir: HardwareIntermediateRepresentation, *, video_url: str, original_prompt=None, project_id=None) -> VideoIterationReview:
        self.video_url = video_url
        return VideoIterationReview(
            summary="Display continuity mismatch found.",
            coherence_score=0.42,
            needs_iteration=True,
            target_namespace="product.mech",
            issues=[
                VideoCoherenceIssue(
                    severity="warning",
                    category="continuity",
                    frame_reference="frame 3",
                    description="The enclosure display moves between shots.",
                    evidence="The display is centered in early frames and left-shifted later.",
                    suggested_correction="Lock the display placement in the enclosure notes.",
                )
            ],
            iteration_instruction="Revise the mechanical enclosure so the display placement remains consistent across the video frames.",
        )


class FakeUrlopenResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")


class ProjectIterationTests(unittest.TestCase):
    def test_required_live_iteration_rejects_metadata_only_fallback(self) -> None:
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="openai", model="missing-model"),
            llm_provider=UnconfiguredProvider(),
            require_live_generation=True,
        )

        with self.assertRaisesRegex(LLMProviderConfigError, "CLI fallback output is disabled"):
            iterator.iterate_project(build_sample_ir(), "Change the enclosure.")

    def test_metadata_only_iteration_records_revision_without_changing_hardware(self) -> None:
        current = build_sample_ir()
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="simulation", model="simulation"),
            use_simulation=True,
        )

        revised = iterator.iterate_project(
            current,
            "Add a waterproof enclosure note.",
            project_id=PROJECT_ID,
            target_namespace="product.mech",
        )

        self.assertEqual(PROJECT_ID, revised.assembly_metadata["project_id"])
        self.assertEqual(2, revised.assembly_metadata["revision"])
        self.assertEqual("product.mech", revised.assembly_metadata["iteration_target_namespace"])
        self.assertEqual("metadata-only", revised.assembly_metadata["iteration_mode"])
        self.assertEqual(
            {
                "instruction": "Add a waterproof enclosure note.",
                "target_namespace": "product.mech",
            },
            {
                "instruction": revised.assembly_metadata["pending_iteration_instructions"][0]["instruction"],
                "target_namespace": revised.assembly_metadata["pending_iteration_instructions"][0]["target_namespace"],
            },
        )
        self.assertEqual("ESP32 Dev Board", revised.components[0].name)
        self.assertEqual(2, len(revised.project_version_history))
        object_metadata = revised.assembly_metadata["project_object"]
        self.assertEqual(2, object_metadata["version"])
        self.assertEqual(2, object_metadata["namespace_versions"]["product.mech"])
        self.assertEqual(1, object_metadata["namespace_versions"]["product.electrical"])
        self.assertTrue(revised.is_valid)

    def test_live_iteration_uses_provider_and_normalizes_metadata(self) -> None:
        current = build_sample_ir()
        current.assembly_metadata["chat_id"] = "chat-soil-monitor"
        model_output = current.model_copy(deep=True)
        model_output.overview.description = "A revised monitor with a waterproof enclosure."
        model_output.assembly_metadata = {"project_id": PROJECT_ID, "revision": 99}
        model_output.project_version_history.append(
            {
                "version": "0.2",
                "revision": 2,
                "description": "Model-supplied duplicate history entry.",
            }
        )
        fake_provider = FakeProvider(model_output)
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="openai", model="gpt-5.5"),
            llm_provider=fake_provider,
        )

        revised = iterator.iterate_project(
            current,
            "Make the enclosure waterproof.",
            original_prompt="soil monitor",
            target_namespace="product.mech",
        )

        self.assertIn("Make the enclosure waterproof.", fake_provider.prompt)
        self.assertIn("Target namespace: product.mech", fake_provider.prompt)
        self.assertEqual(2, revised.assembly_metadata["revision"])
        self.assertEqual("llm", revised.assembly_metadata["iteration_mode"])
        self.assertEqual("product.mech", revised.assembly_metadata["iteration_target_namespace"])
        self.assertEqual("openai", revised.assembly_metadata["iteration_provider"])
        self.assertEqual("gpt-5.5", revised.assembly_metadata["iteration_model"])
        self.assertEqual(PROJECT_ID, revised.assembly_metadata["project_id"])
        self.assertEqual("chat-soil-monitor", revised.assembly_metadata["chat_id"])
        self.assertEqual(2, len(revised.project_version_history))
        self.assertEqual("Make the enclosure waterproof.", revised.project_version_history[-1]["description"])
        object_metadata = revised.assembly_metadata["project_object"]
        self.assertEqual(2, object_metadata["namespace_versions"]["product.mech"])
        self.assertEqual(1, object_metadata["namespace_versions"]["product.electrical"])
        self.assertTrue(revised.is_valid)

    def test_mechanical_iteration_preserves_bom_and_electrical_connectivity(self) -> None:
        current = build_sample_ir()
        model_output = current.model_copy(deep=True)
        model_output.components = []
        model_output.nets = []
        model_output.estimated_current_draw_ma = 999.0
        fake_provider = FakeProvider(model_output)
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="openai", model="gpt-5.5"),
            llm_provider=fake_provider,
        )

        revised = iterator.iterate_project(
            current,
            "Change the product to a curved handheld shape without changing its parts.",
            target_namespace="product.mech",
        )

        self.assertEqual(current.components, revised.components)
        self.assertEqual(current.nets, revised.nets)
        self.assertEqual(current.estimated_current_draw_ma, revised.estimated_current_draw_ma)
        self.assertIn("Keep the existing BOM/components and all electrical connectivity fixed", fake_provider.prompt)

    def test_iteration_preserves_generated_output_metadata_when_model_omits_it(self) -> None:
        current = build_sample_ir()
        current.assembly_metadata.update(
            {
                "image_output_status": "succeeded",
                "product_image_url": "https://example.test/product.png",
                "product_visual_sequence_count": 1,
                "external_research": {
                    "provider": "tavily",
                    "configured": True,
                    "source_count": 2,
                    "error": None,
                },
            }
        )
        model_output = current.model_copy(deep=True)
        model_output.overview.description = "A revised monitor with better docs."
        model_output.assembly_metadata = {"project_id": PROJECT_ID}
        fake_provider = FakeProvider(model_output)
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="openai", model="gpt-5.5"),
            llm_provider=fake_provider,
        )

        revised = iterator.iterate_project(current, "Improve docs.", target_namespace="project.docs")

        self.assertEqual("succeeded", revised.assembly_metadata["image_output_status"])
        self.assertEqual("https://example.test/product.png", revised.assembly_metadata["product_image_url"])
        self.assertEqual(1, revised.assembly_metadata["product_visual_sequence_count"])
        self.assertEqual("tavily", revised.assembly_metadata["external_research"]["provider"])

    def test_iteration_context_redacts_data_urls(self) -> None:
        current = build_sample_ir()
        current.assembly_metadata["product_image_data"] = "data:image/png;base64," + ("a" * 200)

        compact = compact_hardware_ir_for_iteration(current)

        self.assertEqual("<redacted data url: 222 chars>", compact["assembly_metadata"]["product_image_data"])

    def test_project_object_exposes_versioned_namespaces(self) -> None:
        current = build_sample_ir()

        project_object = build_project_object(current)

        self.assertEqual(PROJECT_ID, project_object.object_id)
        self.assertEqual(1, project_object.version)
        product_mech = project_object.get_namespace("product.mech")
        project_docs = project_object.get_namespace("project.docs")
        self.assertIsNotNone(product_mech)
        self.assertIsNotNone(project_docs)
        assert product_mech is not None
        assert project_docs is not None
        self.assertEqual(1, product_mech.version)
        self.assertEqual("product", product_mech.scope)
        self.assertEqual("Project Documentation", project_docs.label)

    def test_database_project_object_attachment_preserves_iteration_target_namespace(self) -> None:
        current = build_sample_ir()
        current.assembly_metadata.update(
            {
                "revision": 2,
                "previous_revision": 1,
                "iteration_target_namespace": "product.electrical",
                "project_object": {
                    "namespace_versions": {
                        "product.overview": 1,
                        "product.electrical": 2,
                        "product.mech": 1,
                        "project.docs": 1,
                    }
                },
            }
        )

        normalized = _hardware_ir_with_project_id(PROJECT_ID, current.model_dump(mode="json"))
        versions = normalized["assembly_metadata"]["project_object"]["namespace_versions"]

        self.assertEqual(2, versions["product.electrical"])
        self.assertEqual(1, versions["product.mech"])
        self.assertEqual(1, versions["project.docs"])

    def test_project_object_exposes_typed_attribute_and_item_metadata(self) -> None:
        current = build_sample_ir()

        project_object = build_project_object(current)
        components = project_object.get_attribute("product.electrical", "components")

        self.assertIsNotNone(components)
        assert components is not None
        self.assertEqual("components", components.name)
        self.assertEqual("Components", components.label)
        self.assertEqual("product.electrical.components", components.meta.source_path)
        self.assertEqual("array", components.meta.value_type)
        self.assertEqual("component", components.meta.item_kind)
        self.assertEqual(1, components.meta.item_count)

        component = components.get_item("U1")
        self.assertIsNotNone(component)
        assert component is not None
        self.assertEqual("U1", component.item_id)
        self.assertEqual("ESP32 Dev Board", component.label)
        self.assertEqual("component", component.item_kind)
        self.assertEqual("product.electrical.components[0]", component.meta.source_path)
        self.assertEqual("U1", component.meta.ref_des)
        self.assertEqual("Microcontroller", component.meta.category)
        self.assertEqual("ESP32-WROOM-32D", component.meta.part_number)

    def test_namespace_payload_can_target_artifact_domain(self) -> None:
        current = build_sample_ir()

        self.assertEqual("product.mech", normalize_project_namespace(" Product.Mech "))
        self.assertIn("mechanical", namespace_payload(current, "product.mech"))
        self.assertIn("assembly", namespace_payload(current, "project.docs"))

    def test_self_correction_agent_targets_electrical_namespace(self) -> None:
        current = build_sample_ir()
        current.nets[0].pins.append(PinReference(ref_des="U1", pin_id="GND"))
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="simulation", model="simulation"),
            use_simulation=True,
        )
        agent = ProjectSelfCorrectionAgent(iterator=iterator)

        plan = agent.plan_correction(current)
        revised = agent.correct_project(current, project_id=PROJECT_ID)

        self.assertEqual("product.electrical", plan.target_namespace)
        self.assertEqual(1, plan.critical_issue_count)
        self.assertIn("Short Circuit", plan.instruction)
        self.assertEqual("product.electrical", revised.assembly_metadata["iteration_target_namespace"])
        self.assertEqual(2, revised.assembly_metadata["project_object"]["namespace_versions"]["product.electrical"])

    def test_self_correction_agent_includes_metadata_output_findings(self) -> None:
        current = build_sample_ir()
        current.assembly_metadata.update(
            {
                "external_research": {
                    "provider": "firecrawl",
                    "configured": True,
                    "source_count": 0,
                    "error": "MCP request timed out: initialize",
                },
                "image_output_requested": True,
                "image_output_status": "failed",
                "image_output_error": "invalid api key",
            }
        )
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="simulation", model="simulation"),
            use_simulation=True,
        )
        agent = ProjectSelfCorrectionAgent(iterator=iterator)

        plan = agent.plan_correction(current)

        self.assertEqual("project.docs", plan.target_namespace)
        self.assertEqual(3, plan.output_issue_count)
        self.assertIn("MCP request timed out", plan.instruction)
        self.assertIn("Image generation was requested", plan.instruction)

    def test_video_self_correction_agent_records_review_metadata(self) -> None:
        current = build_sample_ir()
        review_client = FakeVideoReviewClient()
        iterator = ProjectIterator(
            runtime_config=LLMRuntimeConfig(provider="simulation", model="simulation"),
            use_simulation=True,
        )
        agent = FireworksVideoSelfCorrectionAgent(review_client=review_client, iterator=iterator)

        revised, review = agent.correct_project_from_video(
            current,
            video_url="https://example.test/render.mp4",
            original_prompt="soil monitor",
            project_id=PROJECT_ID,
        )

        self.assertEqual("https://example.test/render.mp4", review_client.video_url)
        self.assertEqual("Display continuity mismatch found.", review.summary)
        self.assertEqual("product.mech", revised.assembly_metadata["iteration_target_namespace"])
        self.assertEqual(2, revised.assembly_metadata["revision"])
        self.assertEqual("fake-fireworks-vlm", revised.assembly_metadata["video_self_correction"]["review_model"])
        self.assertEqual(1, len(revised.assembly_metadata["video_self_correction"]["issues"]))
        self.assertEqual(
            {
                "summary": "Display continuity mismatch found.",
                "coherence_score": 0.42,
                "issue_count": 1,
                "review_model": "fake-fireworks-vlm",
            },
            revised.assembly_metadata["last_iteration"]["video_review"],
        )
        self.assertEqual(
            "Display continuity mismatch found.",
            revised.project_version_history[-1]["video_review"]["summary"],
        )

    def test_video_self_correct_request_requires_http_video_url(self) -> None:
        request = VideoSelfCorrectRequest(
            video_url=" https://example.test/render.mp4 ",
            namespace=" product.mech ",
            review_model=" fireworks/custom-review ",
        )

        self.assertEqual("https://example.test/render.mp4", request.video_url)
        self.assertEqual("product.mech", request.namespace)
        self.assertEqual("fireworks/custom-review", request.review_model)
        with self.assertRaises(ValidationError):
            VideoSelfCorrectRequest(video_url="file:///tmp/render.mp4")

    def test_fireworks_video_review_error_message_names_core_package(self) -> None:
        message = _fireworks_http_error_message(
            status_code=404,
            model="accounts/fireworks/models/qwen3-omni-30b-a3b-instruct",
            body=(
                '{"error":{"message":"Model not found, inaccessible, and/or not deployed",'
                '"param":"model","code":"NOT_FOUND","type":"error"},"request_id":"chatcmpl-test"}'
            ),
        )

        self.assertIn("forma_core.video_review", message)
        self.assertIn("accounts/fireworks/models/qwen3-omni-30b-a3b-instruct", message)
        self.assertIn("Model not found, inaccessible, and/or not deployed", message)
        self.assertIn("FIREWORKS_VIDEO_REVIEW_MODEL", message)
        self.assertIn("chatcmpl-test", message)

    def test_fireworks_video_review_http_error_is_logged_from_core_package(self) -> None:
        current = build_sample_ir()
        body = (
            b'{"error":{"message":"Model not found, inaccessible, and/or not deployed",'
            b'"param":"model","code":"NOT_FOUND","type":"error"},"request_id":"chatcmpl-test"}'
        )
        http_error = urllib.error.HTTPError(
            "https://api.fireworks.ai/inference/v1/chat/completions",
            404,
            "Not Found",
            {},
            io.BytesIO(body),
        )
        client = FireworksVideoReviewClient(api_key="test-key", model="accounts/fireworks/models/qwen3-omni-30b-a3b-instruct")

        with (
            patch(
                "forma_core.video_review.prepare_video_for_fireworks_native_review",
                return_value=FireworksPreparedVideo(video_data_url="data:video/mp4;base64,ZmFrZQ=="),
            ),
            patch("forma_core.video_review.urllib.request.urlopen", side_effect=http_error),
            self.assertLogs("forma_core.video_review", level="ERROR") as logs,
            self.assertRaises(LLMProviderOutputError),
        ):
            client.review_video(current, video_url="https://example.test/render.mp4", project_id=PROJECT_ID)

        log_output = "\n".join(logs.output)
        self.assertIn("ERROR:forma_core.video_review", log_output)
        self.assertIn("forma_core.video_review Fireworks video review failed", log_output)
        self.assertIn("NOT_FOUND", log_output)
        self.assertIn("chatcmpl-test", log_output)

    def test_fireworks_video_review_unstructured_content_uses_typed_fallback(self) -> None:
        current = build_sample_ir()
        response = FakeUrlopenResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "The display jumps between the center and left side of the enclosure. Lock the display placement."
                        }
                    }
                ]
            }
        )
        client = FireworksVideoReviewClient(
            api_key="test-key",
            model="accounts/fireworks/models/kimi-k2p6",
            input_mode="frames",
        )

        with (
            patch("forma_core.video_review.sample_video_frames", return_value=[b"fake-jpeg"]),
            patch("forma_core.video_review.urllib.request.urlopen", return_value=response),
            self.assertLogs("forma_core.video_review", level="WARNING") as logs,
        ):
            review = client.review_video(current, video_url="https://example.test/render.mp4", project_id=PROJECT_ID)

        self.assertEqual("product.mech", review.target_namespace)
        self.assertEqual(1, len(review.issues))
        self.assertIn("display jumps", review.summary)
        self.assertIn("Lock the display placement", review.iteration_instruction)
        self.assertIn("using fallback review", "\n".join(logs.output))
        self.assertIn("finding_preview", "\n".join(logs.output))

    def test_message_content_text_uses_reasoning_when_content_is_empty(self) -> None:
        self.assertEqual(
            "Reasoning text with findings.",
            _message_content_text({"content": "", "reasoning_content": "Reasoning text with findings."}),
        )

    def test_unstructured_review_distillation_strips_model_preamble(self) -> None:
        raw = (
            "The user wants me to review a generated hardware video against the current HardwareIntermediateRepresentation. "
            "I need to inspect the video frames and compare them with the HardwareIntermediateRepresentation JSON provided. "
            "First, let me understand what the HardwareIntermediateRepresentation describes. "
            "Frame 1 shows a full FDM 3D printer with a gantry, bed, nozzle, and LCD control box. "
            "This is a major mismatch because the HardwareIntermediateRepresentation only contains an ESP32, OLED display, servo motors, resistor, and LiPo battery."
        )

        distilled = _distill_unstructured_review_text(raw)

        self.assertNotIn("The user wants me", distilled)
        self.assertNotIn("I need to inspect", distilled)
        self.assertIn("major mismatch", distilled)
        self.assertIn("HardwareIntermediateRepresentation only contains", distilled)

    def test_unstructured_review_target_prefers_visual_over_visible_battery(self) -> None:
        text = (
            "Frame 6 shows a lower-right electronics pod with ESP32, servo, and battery. "
            "The gear looks like an impeller and the LCD text is visually inconsistent with the OLED display."
        )

        self.assertEqual("product.mech", _unstructured_review_target_namespace(text))

    def test_fireworks_video_review_client_builds_native_deployment_model_path(self) -> None:
        client = FireworksVideoReviewClient(
            api_key="test-key",
            account_id="caid",
            deployment_id="video-review",
        )

        self.assertEqual(
            f"accounts/caid/models/{DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL_SLUG}#accounts/caid/deployments/video-review",
            client.model,
        )
        self.assertEqual("native_video", client.input_mode)
        self.assertTrue(client.include_audio)
        self.assertTrue(client.deployment_configured)
        self.assertIn("molmo2-8b", FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS)

    def test_fireworks_video_review_client_defaults_to_working_frame_model(self) -> None:
        client = FireworksVideoReviewClient(api_key="test-key")

        self.assertEqual(f"accounts/fireworks/models/{DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG}", client.model)
        self.assertEqual("kimi-k2p6", DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG)
        self.assertEqual("frames", client.input_mode)
        self.assertFalse(client.include_audio)
        self.assertTrue(client.deployment_configured)
        self.assertIn("kimi-k2p6", FIREWORKS_VIDEO_REVIEW_FRAME_MODELS)


if __name__ == "__main__":
    unittest.main()
