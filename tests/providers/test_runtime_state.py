from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from forma_core.config.runtime import (
    RuntimeConfigurationError,
    authoring_mode_enabled,
    configured_agent_runtimes,
    deployment_mode,
    development_mode_enabled,
    hosted_chat_enabled,
    runtime_state,
)


class RuntimeStateTests(unittest.TestCase):
    def test_unset_runtime_defaults_to_local_and_not_development(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("local", deployment_mode())
            self.assertFalse(development_mode_enabled())
            self.assertEqual(
                {
                    "deployment_mode": "local",
                    "development_mode": False,
                    "legacy_development_mode": None,
                },
                runtime_state(),
            )

    def test_valid_deployment_modes_are_accepted(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "local"}, clear=True):
            self.assertEqual("local", deployment_mode())
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "hosted"}, clear=True):
            self.assertEqual("hosted", deployment_mode())

    def test_invalid_deployment_mode_fails(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "staging"}, clear=True):
            with self.assertRaisesRegex(RuntimeConfigurationError, "Expected 'local' or 'hosted'"):
                deployment_mode()

    def test_invalid_development_mode_fails(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEVELOPMENT_MODE": "yes"}, clear=True):
            with self.assertRaisesRegex(RuntimeConfigurationError, "Expected 'true' or 'false'"):
                development_mode_enabled()

    def test_hosted_development_combination_fails(self) -> None:
        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "hosted", "FORMA_DEVELOPMENT_MODE": "true"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeConfigurationError, "cannot be combined"):
                runtime_state()

    def test_hosted_mode_is_valid_when_development_is_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "hosted", "FORMA_DEVELOPMENT_MODE": "false"},
            clear=True,
        ):
            self.assertEqual("hosted", runtime_state()["deployment_mode"])

    def test_legacy_development_alias_is_still_supported(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEV_MODE": "true"}, clear=True):
            self.assertTrue(development_mode_enabled())

    def test_hosted_chat_defaults_to_enabled_locally_and_disabled_when_hosted(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "local"}, clear=True):
            self.assertTrue(hosted_chat_enabled())
        with patch.dict(os.environ, {"FORMA_DEPLOYMENT_MODE": "hosted"}, clear=True):
            self.assertFalse(hosted_chat_enabled())

    def test_hosted_chat_flag_can_be_reversed_explicitly(self) -> None:
        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "hosted", "FORMA_HOSTED_CHAT_ENABLED": "true"},
            clear=True,
        ):
            self.assertTrue(hosted_chat_enabled())

    def test_authoring_mode_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(authoring_mode_enabled())

    def test_authoring_mode_flag_can_be_enabled_explicitly(self) -> None:
        with patch.dict(os.environ, {"FORMA_AUTHORING_MODE_ENABLED": "true"}, clear=True):
            self.assertTrue(authoring_mode_enabled())

    def test_agent_runtime_inventory_uses_labels_and_keeps_default_first(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORMA_OPENCODE_CONNECTOR_ID": "mini-pc-1",
                "FORMA_AGENT_RUNTIMES": "laptop=Local PC,cloud=Cloud Runtime",
            },
            clear=True,
        ):
            self.assertEqual(
                [
                    {"id": "mini-pc-1", "label": "Mini Pc 1"},
                    {"id": "laptop", "label": "Local PC"},
                    {"id": "cloud", "label": "Cloud Runtime"},
                ],
                configured_agent_runtimes(),
            )

    def test_agent_runtime_inventory_deduplicates_default_connector(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORMA_OPENCODE_CONNECTOR_ID": "mini-pc-1",
                "FORMA_AGENT_RUNTIMES": "mini-pc-1=CAID Server,laptop=Local PC",
            },
            clear=True,
        ):
            self.assertEqual(
                [
                    {"id": "mini-pc-1", "label": "CAID Server"},
                    {"id": "laptop", "label": "Local PC"},
                ],
                configured_agent_runtimes(),
            )


if __name__ == "__main__":
    unittest.main()
