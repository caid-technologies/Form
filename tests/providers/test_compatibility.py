from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from apps.api.compatibility import require_client_compatibility
from apps.api.main import app
from forma_cli.credentials import CredentialStore
from forma_cli.sdk import FormaAPIClient
from forma_core._version import __version__
from forma_core.config.compatibility import (
    CURRENT_PROTOCOL_VERSION,
    CompatibilityMetadata,
    CompatibilityStatus,
    evaluate_compatibility,
)


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _metadata(**overrides: object) -> dict[str, object]:
    return {
        "latest_version": __version__,
        "minimum_supported_version": __version__,
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "hardware_ir_version": "0.2",
        "supported_hardware_ir_versions": ["0.2"],
        **overrides,
    }


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/cli/projects",
            "raw_path": b"/cli/projects",
            "query_string": b"",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "server": ("testserver", 443),
            "client": ("testclient", 50000),
        }
    )


class CompatibilityPolicyTests(unittest.TestCase):
    def test_current_client_is_current(self) -> None:
        result = evaluate_compatibility(__version__, CompatibilityMetadata.model_validate(_metadata()))

        self.assertEqual(CompatibilityStatus.CURRENT, result.status)

    def test_older_supported_client_gets_update_notice(self) -> None:
        result = evaluate_compatibility(
            "0.3.3",
            CompatibilityMetadata.model_validate(
                _metadata(latest_version="0.3.5", minimum_supported_version="0.3.0")
            ),
        )

        self.assertEqual(CompatibilityStatus.UPDATE_AVAILABLE, result.status)
        self.assertIn("pipx upgrade forma-oss", result.message)

    def test_client_below_minimum_is_blocked(self) -> None:
        result = evaluate_compatibility(
            "0.2.9",
            CompatibilityMetadata.model_validate(_metadata(minimum_supported_version="0.3.0")),
        )

        self.assertEqual(CompatibilityStatus.UNSUPPORTED_CLIENT, result.status)
        self.assertTrue(result.is_blocking)

    def test_protocol_mismatch_is_distinct(self) -> None:
        result = evaluate_compatibility(
            __version__,
            CompatibilityMetadata.model_validate(_metadata(protocol_version=2)),
        )

        self.assertEqual(CompatibilityStatus.UNSUPPORTED_PROTOCOL, result.status)

    def test_sdk_caches_remote_compatibility_and_sends_contract_headers(self) -> None:
        client = FormaAPIClient(base_url="https://api.example.test", credential_store=CredentialStore())
        with patch(
            "forma_cli.sdk.urlopen",
            side_effect=[Response(_metadata()), Response({"ok": True})],
        ) as urlopen:
            result = client.check_compatibility()
            client._request("/health")

        self.assertEqual(CompatibilityStatus.CURRENT, result.status)
        self.assertEqual(2, urlopen.call_count)
        request = urlopen.call_args_list[1].args[0]
        self.assertEqual(__version__, request.get_header("X-forma-client-version"))
        self.assertEqual(str(CURRENT_PROTOCOL_VERSION), request.get_header("X-forma-protocol-version"))

    def test_sdk_force_refresh_does_not_trust_stale_compatible_metadata(self) -> None:
        client = FormaAPIClient(base_url="https://api.example.test", credential_store=CredentialStore())
        with patch(
            "forma_cli.sdk.urlopen",
            side_effect=[
                Response(_metadata()),
                Response(_metadata(minimum_supported_version="0.4.0")),
            ],
        ):
            self.assertEqual(CompatibilityStatus.CURRENT, client.check_compatibility().status)
            refreshed = client.check_compatibility(force=True)

        self.assertEqual(CompatibilityStatus.UNSUPPORTED_CLIENT, refreshed.status)

    def test_local_sdk_does_not_require_remote_compatibility(self) -> None:
        client = FormaAPIClient(base_url="http://127.0.0.1:8000", credential_store=CredentialStore())

        result = client.check_compatibility()

        self.assertEqual(CompatibilityStatus.REMOTE_VERSION_UNAVAILABLE, result.status)

    def test_server_rejects_explicitly_unsupported_client(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORMA_HOSTED_LATEST_VERSION": "0.3.4",
                "FORMA_HOSTED_MINIMUM_SUPPORTED_VERSION": "0.3.0",
            },
            clear=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                require_client_compatibility(
                    _request(
                        {
                            "X-Forma-Client-Version": "0.2.9",
                            "X-Forma-Protocol-Version": str(CURRENT_PROTOCOL_VERSION),
                        }
                    )
                )

        self.assertEqual(426, raised.exception.status_code)
        self.assertEqual("UNSUPPORTED_CLIENT_VERSION", raised.exception.detail["code"])

    def test_cli_route_applies_server_compatibility_backstop(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORMA_HOSTED_LATEST_VERSION": "0.3.4",
                "FORMA_HOSTED_MINIMUM_SUPPORTED_VERSION": "0.3.0",
            },
            clear=False,
        ):
            response = TestClient(app).post(
                "/cli/device/authorize",
                headers={
                    "X-Forma-Client-Version": "0.2.9",
                    "X-Forma-Protocol-Version": str(CURRENT_PROTOCOL_VERSION),
                },
            )

        self.assertEqual(426, response.status_code)
        self.assertEqual("UNSUPPORTED_CLIENT_VERSION", response.json()["detail"]["code"])

    def test_public_endpoint_exposes_machine_readable_contract(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORMA_HOSTED_LATEST_VERSION": "0.3.5",
                "FORMA_HOSTED_MINIMUM_SUPPORTED_VERSION": "0.3.0",
                "FORMA_HOSTED_PROTOCOL_VERSION": "2",
            },
            clear=False,
        ):
            response = TestClient(app).get("/forma/version")

        self.assertEqual(200, response.status_code)
        self.assertEqual("0.3.5", response.json()["latest_version"])
        self.assertEqual(2, response.json()["protocol_version"])


if __name__ == "__main__":
    unittest.main()
