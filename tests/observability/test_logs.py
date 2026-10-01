from __future__ import annotations

import errno
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apps.api import logging_config
from forma_core.logs import backend_log_payload, redact_log_line, resolve_backend_log_path


class BackendLogCoreTests(unittest.TestCase):
    def test_resolve_backend_log_path_uses_env_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = resolve_backend_log_path(env={"BACKEND_LOG_FILE": "logs/backend.log"}, cwd=Path(tmp_dir))

        self.assertEqual(Path(tmp_dir, "logs/backend.log").resolve(), path)

    def test_resolve_backend_log_path_returns_none_when_unset(self) -> None:
        self.assertIsNone(resolve_backend_log_path(env={}))

    def test_redact_log_line_hides_provider_keys(self) -> None:
        line = "openai=sk-testsecret123456 firecrawl=fc-abcdefghijklmnop visible=ok"

        redacted = redact_log_line(line)

        self.assertNotIn("sk-testsecret123456", redacted)
        self.assertNotIn("fc-abcdefghijklmnop", redacted)
        self.assertIn("visible=ok", redacted)

    def test_backend_log_payload_reports_missing_configuration(self) -> None:
        payload = backend_log_payload(env={})

        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["configured"])
        self.assertEqual("BACKEND_LOG_FILE is not configured.", payload["message"])

    def test_backend_log_payload_reports_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "missing.log"
            payload = backend_log_payload(log_path=missing_path)

        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["configured"])
        self.assertEqual(str(missing_path), payload["path"])
        self.assertEqual("Backend log file does not exist yet.", payload["message"])

    def test_backend_log_payload_tails_and_redacts_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "backend.log"
            log_path.write_text(
                "\n".join(
                    [
                        "first line",
                        "secret sk-testsecret123456",
                        "last line",
                    ]
                ),
                encoding="utf-8",
            )

            payload = backend_log_payload(log_path=log_path, line_limit=2, byte_limit=500_000)

        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["configured"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(2, payload["line_count"])
        self.assertEqual(["secret <redacted>", "last line"], payload["lines"])

    def test_serverless_log_file_falls_back_to_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.dict(os.environ, {"VERCEL": "1", "TMPDIR": tmp_dir}, clear=False):
                fallback = logging_config._tmp_log_fallback_path(Path("/var/task/forma-backend.log"))

        self.assertEqual(Path(tmp_dir, "forma-backend.log").resolve(), fallback)

    def test_vercel_logging_uses_tmp_without_attempting_read_only_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(os.environ, {
            "BACKEND_LOG_FILE": "/var/task/forma-backend.log", "VERCEL": "1", "TMPDIR": tmp_dir,
        }, clear=True), mock.patch.object(logging_config, "_attach_file_handlers") as attach:
            with self.assertNoLogs(logging_config.__name__, level="WARNING"):
                logging_config.configure_backend_logging()
            expected = Path(tmp_dir, "forma-backend.log").resolve()
            self.assertEqual(expected, attach.call_args.args[0])
            self.assertEqual(str(expected), os.environ["BACKEND_LOG_FILE"])
            attach.assert_called_once()

    def test_serverless_logging_preserves_path_already_inside_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(os.environ, {
            "VERCEL": "1", "TMPDIR": tmp_dir,
        }, clear=True):
            self.assertIsNone(logging_config._tmp_log_fallback_path(Path(tmp_dir, "logs/backend.log")))

    def test_file_loggers_share_one_non_rotating_handler(self) -> None:
        loggers = [
            logging.getLogger(),
            logging.getLogger("uvicorn.error"),
            logging.getLogger("uvicorn.access"),
        ]
        formatter = logging.Formatter(logging_config.DEFAULT_LOG_FORMAT)

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir, "backend.log").resolve()
            logging_config._attach_file_handlers(log_path, logging.INFO, formatter, ())
            handlers = [logging_config._handler_for_path(logger, log_path) for logger in loggers]

            try:
                self.assertIsNotNone(handlers[0])
                self.assertTrue(all(handler is handlers[0] for handler in handlers))
                self.assertIs(type(handlers[0]), logging.FileHandler)
            finally:
                for logger in loggers:
                    if handlers[0] in logger.handlers:
                        logger.removeHandler(handlers[0])
                handlers[0].close()

    def test_configure_backend_logging_does_not_crash_on_read_only_log_file(self) -> None:
        calls: list[Path] = []

        def fake_attach_file_handlers(
            log_path: Path,
            level: int,
            formatter: logging.Formatter,
            namespaces: tuple[str, ...],
        ) -> None:
            calls.append(log_path)
            if len(calls) == 1:
                raise OSError(errno.EROFS, "Read-only file system")

        with tempfile.TemporaryDirectory() as tmp_dir:
            fallback_path = Path(tmp_dir, "forma-backend.log").resolve()
            env = {
                "BACKEND_LOG_FILE": "forma-backend.log",
                "TMPDIR": tmp_dir,
                "AWS_LAMBDA_FUNCTION_NAME": "forma",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(logging_config, "_attach_file_handlers", side_effect=fake_attach_file_handlers):
                    logging_config.configure_backend_logging()

                self.assertEqual(str(fallback_path), os.environ["BACKEND_LOG_FILE"])

        self.assertEqual(Path("forma-backend.log").resolve(), calls[0])
        self.assertEqual(fallback_path, calls[1])


if __name__ == "__main__":
    unittest.main()
