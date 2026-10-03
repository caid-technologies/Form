from __future__ import annotations

import unittest
import os
from unittest.mock import patch

from apps.api.cli_auth_store import (
    CliIdentity,
    approve_device,
    create_device_session,
    device_status,
    exchange_device,
    refresh_access,
    resolve_access_token,
    revoke_tokens,
)


class CliAuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._storage = patch.dict(os.environ, {"FORMA_CLI_AUTH_STORAGE": "memory"})
        self._storage.start()
        self.addCleanup(self._storage.stop)

    def test_device_approval_issues_opaque_access_and_refresh_tokens(self) -> None:
        session = create_device_session()
        self.assertEqual("pending", device_status(session.device_code))

        self.assertTrue(
            approve_device(
                session.user_code,
                CliIdentity(subject="user-1", provider="clerk", email="user@example.test"),
            )
        )
        issued = exchange_device(session.device_code)
        self.assertIsNotNone(issued)
        access_token, refresh_token, _ = issued or ("", "", 0)
        self.assertNotEqual(session.device_code, access_token)
        self.assertEqual("user-1", (resolve_access_token(access_token) or CliIdentity("", "")).subject)

        refreshed = refresh_access(refresh_token)
        self.assertIsNotNone(refreshed)
        refreshed_access, _, _ = refreshed or ("", "", 0)
        self.assertEqual("user-1", (resolve_access_token(refreshed_access) or CliIdentity("", "")).subject)

        revoke_tokens(refresh_token=refresh_token)
        self.assertIsNone(resolve_access_token(access_token))
        self.assertIsNone(resolve_access_token(refreshed_access))

    def test_device_code_cannot_be_exchanged_twice(self) -> None:
        session = create_device_session()
        approve_device(session.user_code, CliIdentity(subject="user-2", provider="local"))
        self.assertIsNotNone(exchange_device(session.device_code))
        self.assertIsNone(exchange_device(session.device_code))


if __name__ == "__main__":
    unittest.main()
