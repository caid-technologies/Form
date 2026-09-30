import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError

from apps.api.opencode_images import ImageToolError, generate_project_image
from apps.api.opencode_mcp import _compile, _tool_result, handle_opencode_mcp_json_rpc
from apps.api.auth import UserContext
from forma_core.image_providers import GeneratedImage, GMIImageProvider, OpenAIImageProvider, build_image_provider
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.opencode.store import OpenCodeStore
from forma_core.opencode.models import GenerateImageArguments, McpJsonRpcRequest
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


class ImageToolTests(unittest.TestCase):
    def setUp(self):
        self.project_id = str(uuid4())
        self.cap = ConnectorCapability("mini", "session", self.project_id, "owner", 2_000_000_000, "nonce", frozenset({"mcp"}))
        store = OpenCodeStore(":memory:")
        self.addCleanup(store.close)
        store.create_session(session_id="session", connector_id="mini", owner_user_id="owner", project_id=self.project_id)
        self.enterContext(patch("apps.api.opencode_api.OPENCODE_STORE", store))
        self.args = GenerateImageArguments(prompt="A concept render of the bracket", request_id="image-1")
        self.source = SimpleNamespace(revision_id="before", state=HardwareIntermediateRepresentation(assembly_metadata={"source_prompt": "Build a bracket"}))
        self.current = SimpleNamespace(revision_id="latest", state=HardwareIntermediateRepresentation(assembly_metadata={"newer_edit": "preserved", "product_visual_sequence": [{"data": "old image"}]}))
        self.saved = None
        self.latest = self.enterContext(patch("apps.api.opencode_images.get_latest_project_revision", side_effect=lambda *_: self.source if self.latest.call_count == 1 else self.current))
        self.lookup = self.enterContext(patch("apps.api.opencode_images.get_project_revision_by_source_job", side_effect=lambda *_: self.saved))
        self.provider = self.enterContext(patch("apps.api.opencode_images.build_image_provider"))
        self.provider.return_value.is_configured = True
        self.provider.return_value.generate_project_image.return_value = GeneratedImage(
            data_url="data:image/png;base64,aW1hZ2U=", provider="openai", model="gpt-image-2", size="1024x1024", prompt="private generated prompt",
        )
        self.upload = self.enterContext(patch("apps.api.opencode_images.upload_image_to_supabase_s3", return_value=None))
        def persist(project, arguments, user):
            self.assertEqual(self.project_id, arguments["project_id"])
            self.assertEqual("owner", user.owner_user_id)
            self.saved = SimpleNamespace(revision_id="saved", state=project)
        self.persist = self.enterContext(patch("apps.api.opencode_images._persist_mcp_compile", side_effect=persist))

    def test_closed_session_prevents_saving_a_generated_image(self):
        def closed():
            raise PermissionError("The authoring session is no longer active.")
        with self.assertRaises(PermissionError):
            generate_project_image(self.args, self.cap, before_save=closed)
        self.persist.assert_not_called()

    def test_image_saves_to_current_project_and_retry_does_not_regenerate(self):
        result = generate_project_image(self.args, self.cap)
        self.assertEqual("gpt-image-2", result["model"])
        self.assertEqual("before", result["source_revision_id"])
        self.assertEqual("preserved", self.saved.state.assembly_metadata["newer_edit"])
        self.assertIn("product_image_data", self.saved.state.assembly_metadata)
        self.assertNotIn("product_visual_sequence", self.saved.state.assembly_metadata)
        self.assertNotIn("base64", str(result))
        self.assertNotIn("private generated prompt", str(result))
        self.assertEqual(result, generate_project_image(self.args, self.cap))
        self.provider.return_value.generate_project_image.assert_called_once_with(self.args.prompt, self.source.state)
        self.upload.assert_called_once()
        self.persist.assert_called_once()
        with self.assertRaises(ImageToolError) as conflict:
            generate_project_image(self.args.model_copy(update={"prompt": "different"}), self.cap)
        self.assertEqual("image_request_conflict", conflict.exception.code)

    def test_remote_storage_metadata_is_saved_without_inline_image(self):
        self.upload.return_value = SimpleNamespace(metadata=lambda _: {"product_image_url": "https://storage.example/image", "product_image_s3_key": "project/image"})
        generate_project_image(self.args, self.cap)
        metadata = self.saved.state.assembly_metadata
        self.assertEqual("project/image", metadata["product_image_s3_key"])
        self.assertNotIn("product_image_data", metadata)

    def test_invalid_image_response_is_rejected_even_when_storage_is_local(self):
        self.provider.return_value.generate_project_image.return_value.data_url = "file:///unexpected/image"
        with self.assertRaises(ImageToolError):
            generate_project_image(self.args, self.cap)
        self.upload.assert_not_called()
        self.persist.assert_not_called()

    def test_missing_project_or_credentials_never_calls_provider(self):
        self.latest.side_effect = None
        self.latest.return_value = None
        with self.assertRaises(ImageToolError):
            generate_project_image(self.args, self.cap)
        self.provider.assert_not_called()
        self.latest.return_value = self.source
        self.provider.return_value.is_configured = False
        with self.assertRaises(ImageToolError):
            generate_project_image(self.args, self.cap)
        self.provider.return_value.generate_project_image.assert_not_called()
        self.persist.assert_not_called()

    def test_provider_and_storage_failures_do_not_persist_or_expose_error_text(self):
        self.provider.return_value.generate_project_image.side_effect = RuntimeError("sk-private-provider-response")
        request = McpJsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forma.opencode.generate_image", "arguments": self.args.model_dump()}})
        result = asyncio.run(handle_opencode_mcp_json_rpc(request, self.cap))
        self.assertTrue(result["result"]["isError"])
        self.assertNotIn("sk-private", str(result))
        self.persist.assert_not_called()
        self.provider.return_value.generate_project_image.side_effect = None
        self.upload.side_effect = RuntimeError("secret storage URL")
        with self.assertRaises(ImageToolError) as failure:
            generate_project_image(self.args, self.cap)
        self.assertEqual("image_storage_failed", failure.exception.code)
        self.persist.assert_not_called()

    def test_tool_rejects_identity_credentials_and_ir_in_image_arguments(self):
        for key in ["project_id", "owner_user_id", "api_key", "base_url", "project_ir", "model", "provider", "image_url"]:
            with self.assertRaises(ValidationError):
                McpJsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forma.opencode.generate_image", "arguments": {**self.args.model_dump(), key: "forbidden"}}})

    def test_followup_ir_edit_preserves_image_without_sending_image_bytes_to_agent(self):
        generate_project_image(self.args, self.cap)
        user = UserContext(provider="test", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
        edited = HardwareIntermediateRepresentation(assembly_metadata={"product_image_model": "forged"})
        with patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=self.saved), patch("apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None), patch("apps.api.opencode_mcp.ensure_native_cad_model"), patch("apps.api.opencode_mcp._persist_mcp_compile"):
            result = _compile(edited, self.project_id, user)
        self.assertIn("product_image_data", edited.assembly_metadata)
        self.assertEqual("gpt-image-2", result["project_ir"]["assembly_metadata"]["product_image_model"])
        self.assertNotIn("product_image_data", result["project_ir"]["assembly_metadata"])
        self.assertNotIn("base64", str(_tool_result(self.saved.state, self.project_id, "saved")))

    def test_gmi_queue_image_is_saved_using_backend_provider_and_deduplicated(self):
        self.provider.side_effect = build_image_provider
        responses = [
            {"request_id": "gmi-1", "status": "queued"},
            {"request_id": "gmi-1", "status": "success", "outcome": {"media_urls": [{"url": "https://cdn.example/render?token=private"}]}},
        ]
        request = McpJsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forma.opencode.generate_image", "arguments": self.args.model_dump()}})
        with patch.dict(os.environ, {
            "IMAGE_PROVIDER": "gmi", "IMAGE_OUTPUT_ENABLED": "false",
            "GMI_CLOUD_API_KEY": "gmi-secret", "GMI_IMAGE_MODEL": "gpt-image-2-generate",
            "OPENAI_API_KEY": "openai-must-not-be-used",
        }, clear=True), patch.object(GMIImageProvider, "_request_queue_json", side_effect=responses) as queue, patch(
            "forma_core.image_providers.time.sleep"
        ), patch("apps.api.opencode_image_data._download_image", return_value=(b"image", "image/jpeg")) as download:
            result = asyncio.run(handle_opencode_mcp_json_rpc(request, self.cap))["result"]
            self.assertNotIn("isError", result)
            self.assertEqual("gmi", result["structuredContent"]["provider"])
            self.assertEqual("gpt-image-2-generate", result["structuredContent"]["model"])
            self.assertEqual(result, asyncio.run(handle_opencode_mcp_json_rpc(request, self.cap))["result"])
        self.assertEqual(["POST", "GET"], [call.kwargs["method"] for call in queue.call_args_list])
        self.assertEqual("gpt-image-2-generate", queue.call_args_list[0].kwargs["payload"]["model"])
        download.assert_called_once_with("https://cdn.example/render?token=private")
        self.assertEqual("data:image/jpeg;base64,aW1hZ2U=", self.saved.state.assembly_metadata["product_image_data"])
        self.assertEqual("image/jpeg", self.saved.state.assembly_metadata["product_image_content_type"])
        self.assertEqual("jpeg", self.saved.state.assembly_metadata["product_image_output_format"])
        self.assertFalse(self.upload.call_args.kwargs["allow_remote_url"])
        self.assertNotIn("cdn.example", str(result))
        self.assertNotIn("secret", str(result))
        self.persist.assert_called_once()

    def test_explicit_gmi_without_credentials_does_not_fall_back_to_openai(self):
        self.provider.side_effect = build_image_provider
        with patch.dict(os.environ, {"IMAGE_PROVIDER": "gmi", "OPENAI_API_KEY": "openai-key"}, clear=True), patch.object(GMIImageProvider, "generate_project_image") as generate:
            with self.assertRaises(ImageToolError) as error:
                generate_project_image(self.args, self.cap)
        self.assertEqual("image_provider_unavailable", error.exception.code)
        generate.assert_not_called()
        self.persist.assert_not_called()

    def test_disabled_provider_cannot_be_enabled_by_the_tool(self):
        self.provider.side_effect = build_image_provider
        with patch.dict(os.environ, {"IMAGE_PROVIDER": "none", "OPENAI_API_KEY": "openai-key", "GMI_API_KEY": "gmi-key"}, clear=True):
            with self.assertRaises(ImageToolError) as error:
                generate_project_image(self.args, self.cap)
        self.assertEqual("image_provider_unavailable", error.exception.code)
        self.persist.assert_not_called()

    def test_openai_configuration_remains_supported(self):
        self.provider.side_effect = build_image_provider
        with patch.dict(os.environ, {"IMAGE_PROVIDER": "openai", "OPENAI_IMAGE_API_KEY": "openai-key"}, clear=True), patch.object(OpenAIImageProvider, "_request_json", return_value={"data": [{"b64_json": "aW1hZ2U="}]}) as request:
            result = generate_project_image(self.args, self.cap)
        self.assertEqual("openai", result["provider"])
        request.assert_called_once()

    def test_image_download_failure_is_sanitized_without_saving(self):
        self.provider.return_value.generate_project_image.return_value.data_url = "https://cdn.example/image?token=private"
        with patch("apps.api.opencode_image_data._download_image", side_effect=RuntimeError("secret provider URL")):
            with self.assertRaises(ImageToolError) as error:
                generate_project_image(self.args, self.cap)
        self.assertNotIn("secret", error.exception.public_message)
        self.assertEqual("image_generation_failed", error.exception.code)
        self.upload.assert_not_called()
        self.persist.assert_not_called()
