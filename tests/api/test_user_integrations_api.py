from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.routing import APIRoute

from apps.api.auth import UserContext, require_user_context
from apps.api.user_integrations_api import (
    ImageModelTestRequest,
    IntegrationUpdateRequest,
    get_user_integrations,
    _store_for_context,
    image_model_test_available,
    router,
    test_image_model as run_image_model_test,
    update_user_integration,
)
from forma_core.user_integrations import SupabaseUserIntegrationStore, SupabaseWorkspaceIntegrationStore


LOCAL_CONTEXT = UserContext(
    provider="local",
    subject="local-dev-user",
    owner_user_id="local-dev-user",
    is_authenticated=True,
    is_admin=True,
)
HOSTED_CONTEXT = UserContext(
    provider="hosted-test",
    subject="user_test",
    owner_user_id="user_test",
    is_authenticated=True,
    is_admin=False,
)


class BrokenIntegrationStore:
    storage_label = "supabase:user_integration_configs/user_test"
    path = "supabase:user_integration_configs/user_test"

    def load(self):
        raise RuntimeError("Stored provider settings were encrypted with a different FORMA_USER_SECRETS_KEY.")

    def update_integration(self, *_args, **_kwargs):
        raise RuntimeError("Supabase write failed")


class PersistThenBrokenReloadStore(BrokenIntegrationStore):
    def update_integration(self, *_args, **_kwargs):
        return None


class UserIntegrationsApiAuthTests(unittest.TestCase):
    def test_local_context_uses_encrypted_workspace_store(self) -> None:
        workspace_store = SupabaseWorkspaceIntegrationStore()
        with patch(
            "apps.api.user_integrations_api.default_integration_store",
            return_value=workspace_store,
        ):
            self.assertIs(workspace_store, _store_for_context(LOCAL_CONTEXT))

    def test_hosted_context_uses_authenticated_user_store(self) -> None:
        user_store = SupabaseUserIntegrationStore("user_test")
        with patch(
            "apps.api.user_integrations_api.UserIntegrationStore.for_user",
            return_value=user_store,
        ) as for_user:
            self.assertIs(user_store, _store_for_context(HOSTED_CONTEXT))
        for_user.assert_called_once_with("user_test")

    def test_hosted_context_without_owner_is_rejected(self) -> None:
        anonymous_context = UserContext(
            provider="hosted-test",
            subject=None,
            owner_user_id=None,
            is_authenticated=False,
            is_admin=False,
        )

        with self.assertRaises(HTTPException) as raised:
            _store_for_context(anonymous_context)

        self.assertEqual(401, raised.exception.status_code)

    def test_user_integration_routes_require_user_context(self) -> None:
        routes = [route for route in router.routes if isinstance(route, APIRoute)]
        self.assertGreaterEqual(len(routes), 4)

        for route in routes:
            dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(require_user_context, dependency_calls, route.path)

    def test_load_failure_is_logged_and_returned_as_structured_error(self) -> None:
        with patch("apps.api.user_integrations_api._store_for_context", return_value=BrokenIntegrationStore()):
            with self.assertLogs("apps.api.user_integrations_api", level="ERROR") as logs:
                with self.assertRaises(HTTPException) as raised:
                    get_user_integrations(HOSTED_CONTEXT)

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("user_integrations_load_failed", raised.exception.detail["code"])
        self.assertIn("different FORMA_USER_SECRETS_KEY", raised.exception.detail["message"])
        self.assertIn("owner_user_id=user_test", "\n".join(logs.output))
        self.assertIn("error_type=RuntimeError", "\n".join(logs.output))

    def test_save_failure_is_logged_and_returned_as_structured_error(self) -> None:
        request = IntegrationUpdateRequest(enabled=True, fields={"api_key": "test-secret"})
        with patch("apps.api.user_integrations_api._store_for_context", return_value=BrokenIntegrationStore()):
            with self.assertLogs("apps.api.user_integrations_api", level="ERROR") as logs:
                with self.assertRaises(HTTPException) as raised:
                    update_user_integration("anthropic", request, HOSTED_CONTEXT)

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("user_integrations_save_failed", raised.exception.detail["code"])
        self.assertIn("Supabase write failed", raised.exception.detail["message"])
        self.assertIn("integration_id=anthropic", "\n".join(logs.output))
        self.assertNotIn("test-secret", "\n".join(logs.output))

    def test_post_save_reload_failure_is_distinguished_from_write_failure(self) -> None:
        request = IntegrationUpdateRequest(enabled=True, fields={"api_key": "test-secret"})
        with patch("apps.api.user_integrations_api._store_for_context", return_value=PersistThenBrokenReloadStore()):
            with self.assertLogs("apps.api.user_integrations_api", level="INFO") as logs:
                with self.assertRaises(HTTPException) as raised:
                    update_user_integration("anthropic", request, HOSTED_CONTEXT)

        output = "\n".join(logs.output)
        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("user_integrations_post_save_reload_failed", raised.exception.detail["code"])
        self.assertIn("update persisted", output)
        self.assertIn("post_save_reload failed", output)
        self.assertNotIn("test-secret", output)

    def test_image_model_test_is_limited_to_local_and_preview(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(image_model_test_available())
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "preview"}, clear=True):
            self.assertTrue(image_model_test_available())
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}, clear=True):
            self.assertFalse(image_model_test_available())
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT": "true"}, clear=True):
            self.assertFalse(image_model_test_available())

    def test_image_model_test_calls_provider_directly(self) -> None:
        class FakeProvider:
            provider_name = "gmi"
            model_name = "seedream-5.0-pro"

            def get_debug_config(self):
                return {"provider": "gmi", "model_name": self.model_name, "configured": True, "api_key": "must-redact"}

            def generate_test_image(self, prompt):
                self.prompt = prompt
                return SimpleNamespace(
                    provider="gmi",
                    model=self.model_name,
                    size="2048x2048",
                    output_format="jpeg",
                    prompt=prompt,
                    prompt_original_length=len(prompt),
                    prompt_final_length=len(prompt),
                    prompt_compacted=False,
                    data_url="data:image/jpeg;base64,ZmFrZQ==",
                )

        provider = FakeProvider()
        request = ImageModelTestRequest(provider="gmi", model="seedream-5.0-pro", prompt="  test render  ")
        with patch.dict(os.environ, {}, clear=True), patch(
            "apps.api.user_integrations_api._store_for_context", return_value=object()
        ), patch("apps.api.user_integrations_api.resolve_user_integration_settings", return_value={}), patch(
            "apps.api.user_integrations_api.build_image_provider", return_value=provider
        ):
            response = run_image_model_test(request, HOSTED_CONTEXT)

        self.assertTrue(response["ok"])
        self.assertEqual("test render", provider.prompt)
        self.assertEqual("data:image/jpeg;base64,ZmFrZQ==", response["image_data_url"])
        self.assertEqual("<redacted>", response["config"]["api_key"])

    def test_image_model_test_route_is_hidden_in_production(self) -> None:
        request = ImageModelTestRequest(provider="gmi", model="seedream-5.0-pro", prompt="test render")
        with patch.dict(os.environ, {"VERCEL": "1", "VERCEL_ENV": "production"}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                run_image_model_test(request, HOSTED_CONTEXT)

        self.assertEqual(404, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
