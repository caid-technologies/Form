from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from apps.api.auth import (
    UserContext,
    optional_deployed_clerk_auth,
    require_deployed_clerk_auth,
    require_mcp_user_context,
)
from apps.api.auth_mode import forma_auth_mode
from forma_core.user_integrations import require_user_secrets_key


def request_with_authorization(value: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", value.encode("utf-8"))],
        }
    )


class AuthModeTests(unittest.IsolatedAsyncioTestCase):
    def test_auth_mode_requires_an_explicit_valid_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertLogs("apps.api.auth_mode", level="CRITICAL"):
                with self.assertRaisesRegex(RuntimeError, "FORMA_AUTH_MODE is required"):
                    forma_auth_mode()
        with patch.dict(os.environ, {"FORMA_AUTH_MODE": "local"}, clear=True):
            self.assertEqual("local", forma_auth_mode())
        with patch.dict(os.environ, {"FORMA_AUTH_MODE": "optional"}, clear=True):
            with self.assertLogs("apps.api.auth_mode", level="CRITICAL"):
                with self.assertRaisesRegex(RuntimeError, "Expected 'local' or 'clerk'"):
                    forma_auth_mode()

    async def test_local_mode_ignores_bearer_tokens(self) -> None:
        request = request_with_authorization("Bearer invalid-token")
        with patch.dict(os.environ, {"FORMA_AUTH_MODE": "local"}, clear=True):
            self.assertIsNone(await require_deployed_clerk_auth(request))
            self.assertIsNone(await optional_deployed_clerk_auth(request))

    async def test_mcp_api_key_authorizes_without_a_clerk_session(self) -> None:
        api_key = "n" * 32
        request = request_with_authorization(f"Bearer {api_key}")
        with patch.dict(
            os.environ,
            {"FORMA_AUTH_MODE": "clerk", "FORMA_MCP_API_KEY": api_key},
            clear=True,
        ):
            context = await require_mcp_user_context(request)

        self.assertEqual("mcp-api-key", context.provider)
        self.assertTrue(context.is_admin)

    async def test_short_mcp_api_key_falls_back_to_admin_auth(self) -> None:
        fallback_context = UserContext(
            provider="clerk",
            subject="admin-user",
            owner_user_id="admin-user",
            is_authenticated=True,
            is_admin=True,
        )
        fallback = AsyncMock(return_value=fallback_context)
        request = request_with_authorization("Bearer short-token")
        with patch.dict(
            os.environ,
            {
                "FORMA_DEPLOYMENT_MODE": "local",
                "FORMA_AUTH_MODE": "local",
                "FORMA_MCP_API_KEY": "short-token",
            },
            clear=True,
        ), patch(
            "apps.api.auth.require_admin_user_context", fallback
        ):
            context = await require_mcp_user_context(request)

        self.assertIs(fallback_context, context)
        fallback.assert_awaited_once_with(request)

    def test_missing_user_secrets_key_logs_and_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertLogs("forma_core.user_integrations", level="CRITICAL") as logs:
                with self.assertRaisesRegex(RuntimeError, "FORMA_USER_SECRETS_KEY is required"):
                    require_user_secrets_key()
        self.assertIn("required at runtime", "\n".join(logs.output))

    async def test_backend_startup_fails_before_database_initialization_without_key(self) -> None:
        from apps.api import main

        with patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": ""}, clear=False), patch.object(
            main, "init_db"
        ) as init_db:
            with self.assertLogs("forma_core.user_integrations", level="CRITICAL"):
                with self.assertRaisesRegex(RuntimeError, "FORMA_USER_SECRETS_KEY is required"):
                    await main.startup_event()
        init_db.assert_not_called()

    async def test_backend_startup_rejects_hosted_development_before_secrets(self) -> None:
        from apps.api import main

        with patch.dict(
            os.environ,
            {
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_DEVELOPMENT_MODE": "true",
            },
            clear=True,
        ), patch.object(main, "require_user_secrets_key") as require_secrets:
            with self.assertRaisesRegex(RuntimeError, "cannot be combined"):
                await main.startup_event()
        require_secrets.assert_not_called()

    async def test_backend_startup_rejects_invalid_deployment_mode_before_secrets(self) -> None:
        from apps.api import main

        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "staging"},
            clear=True,
        ), patch.object(main, "require_user_secrets_key") as require_secrets:
            with self.assertRaisesRegex(RuntimeError, "Expected 'local' or 'hosted'"):
                await main.startup_event()
        require_secrets.assert_not_called()

    async def test_backend_startup_fails_before_database_initialization_without_redis(self) -> None:
        from apps.api import main

        with patch.dict(
            os.environ,
            {
                "FORMA_AUTH_MODE": "local",
                "FORMA_DEV_MODE": "false",
                "FORMA_USER_SECRETS_KEY": "test-secrets-key",
                "REDIS_URL": "",
                "REDIS_CACHE_PREFIX": "",
            },
            clear=True,
        ), patch.object(main, "init_db") as init_db:
            with self.assertLogs("forma_core.project_list_cache", level="CRITICAL"):
                with self.assertRaisesRegex(RuntimeError, "UPSTASH_REDIS_REST_URL"):
                    await main.startup_event()
        init_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
