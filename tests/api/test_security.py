from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import main
from apps.api.security import cors_origins, validate_image_limits


class SecurityPolicyTests(unittest.TestCase):
    def test_local_cors_defaults_are_explicit(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "local"}, clear=True):
            self.assertEqual(
                ["http://localhost:3000", "http://127.0.0.1:3000"],
                cors_origins(),
            )

    def test_hosted_cors_without_configuration_is_deny_by_default(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "hosted"}, clear=True):
            self.assertEqual([], cors_origins())

    def test_wildcard_credentialed_cors_is_rejected(self) -> None:
        with patch.dict(os.environ, {"FORMA_CORS_ORIGINS": "https://app.example, *"}, clear=True):
            with self.assertRaises(RuntimeError):
                cors_origins()

    def test_invalid_and_oversized_images_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_image_limits("data:image/png;base64,not-base64")
        with patch.dict(os.environ, {"FORMA_MAX_IMAGE_ENCODED_CHARS": "4"}, clear=True):
            with self.assertRaises(ValueError):
                validate_image_limits("ZmFrZQ==")

    def test_cors_allows_local_frontend_and_denies_unknown_origin(self) -> None:
        client = TestClient(main.app)
        allowed = client.options(
            "/",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = client.options(
            "/",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual("http://localhost:3000", allowed.headers.get("access-control-allow-origin"))
        self.assertNotIn("access-control-allow-origin", denied.headers)


if __name__ == "__main__":
    unittest.main()
