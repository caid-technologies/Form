from __future__ import annotations

import sqlite3
import tempfile
import unittest

from forma_core.jobs.store import JobCancelledError, JobMetadataStore
from forma_core.workspaces.projects.models import GenerateProjectRequest


class JobProgressTests(unittest.TestCase):
    def test_sqlite_job_store_persists_progress_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobMetadataStore(f"{directory}/jobs.db", backend="sqlite")
            try:
                store.create_job(
                    job_id="job_frontend_progress",
                    message_id="msg_frontend_progress",
                    correlation_id=None,
                    action="forma.generate_project",
                    sender="frontend",
                    recipient="forma",
                    payload={"prompt": "blink an LED", "workflow": "default"},
                    server_owned=True,
                )

                store.append_progress_event(
                    "job_frontend_progress",
                    {
                        "workflow": "default",
                        "step_id": "intent_parser",
                        "status": "started",
                        "agent": "Intent Parser Agent",
                        "label": "Parsing the hardware idea",
                        "description": "Converting the prompt into a project title, category, and build intent.",
                    },
                )
                job = store.get_job("job_frontend_progress")
            finally:
                store.close()

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(1, len(job["progress_events"]))
        self.assertEqual("intent_parser", job["progress_events"][0]["step_id"])
        self.assertIn("observed_at", job["progress_events"][0])

    def test_generate_request_accepts_safe_client_job_id(self) -> None:
        request = GenerateProjectRequest(prompt="blink", client_job_id="job_frontend_abc-123")

        self.assertEqual("job_frontend_abc-123", request.client_job_id)

    def test_generate_request_accepts_named_stage_retry(self) -> None:
        request = GenerateProjectRequest(
            prompt="retry wiring",
            project_id="11111111-1111-4111-8111-111111111111",
            workflow="web_research",
            retry_stage=" wiring_netlist ",
        )

        self.assertEqual("wiring_netlist", request.retry_stage)

    def test_cancelled_job_stays_cancelled_and_stops_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobMetadataStore(f"{directory}/jobs.db", backend="sqlite")
            try:
                store.create_job(
                    job_id="job_frontend_cancelled",
                    message_id="msg_frontend_cancelled",
                    correlation_id=None,
                    action="forma.generate_project",
                    sender="frontend",
                    recipient="forma",
                    payload={"prompt": "blink an LED", "workflow": "default"},
                    server_owned=True,
                )
                store.mark_running("job_frontend_cancelled")
                cancelled = store.mark_cancelled("job_frontend_cancelled")

                self.assertIsNotNone(cancelled)
                assert cancelled is not None
                self.assertEqual("cancelled", cancelled["status"])
                self.assertIsNotNone(cancelled["completed_at"])
                with self.assertRaises(JobCancelledError):
                    store.append_progress_event(
                        "job_frontend_cancelled",
                        {"workflow": "default", "step_id": "intent_parser", "status": "completed"},
                    )

                store.mark_succeeded("job_frontend_cancelled", {"project_ir": {}})
                store.mark_failed("job_frontend_cancelled", "late failure")
                final_job = store.get_job("job_frontend_cancelled")
            finally:
                store.close()

        self.assertIsNotNone(final_job)
        assert final_job is not None
        self.assertEqual("cancelled", final_job["status"])
        self.assertEqual("Cancelled by user.", final_job["error"])

    def test_generate_request_accepts_external_source_provider(self) -> None:
        request = GenerateProjectRequest(prompt="blink", workflow="web_research", external_source_provider="Firecrawl")

        self.assertEqual("firecrawl", request.external_source_provider)

    def test_generate_request_maps_legacy_external_source_provider_to_firecrawl(self) -> None:
        request = GenerateProjectRequest(prompt="blink", workflow="web_research", external_source_provider="auto")

        self.assertEqual("firecrawl", request.external_source_provider)

    def test_generate_request_accepts_tavily_external_source_provider(self) -> None:
        request = GenerateProjectRequest(prompt="blink", workflow="web_research", external_source_provider="Tavily")

        self.assertEqual("tavily", request.external_source_provider)

    def test_generate_request_rejects_unknown_external_source_provider(self) -> None:
        with self.assertRaises(ValueError):
            GenerateProjectRequest(prompt="blink", workflow="web_research", external_source_provider="duckduckgo")

    def test_generate_request_rejects_unsafe_client_job_id(self) -> None:
        with self.assertRaises(ValueError):
            GenerateProjectRequest(prompt="blink", client_job_id="../bad")

    def test_atomic_job_creation_does_not_replace_existing_row(self) -> None:
        store = JobMetadataStore(":memory:", backend="sqlite")
        try:
            store.create_job(
                job_id="job_atomic",
                message_id="msg_owner",
                correlation_id=None,
                action="forma.debug_config",
                sender="owner-agent",
                recipient="forma",
                payload={"owner_user_id": "owner"},
                server_owned=True,
            )

            with self.assertRaises(sqlite3.IntegrityError):
                store.create_job(
                    job_id="job_atomic",
                    message_id="msg_attacker",
                    correlation_id=None,
                    action="forma.debug_config",
                    sender="attacker-agent",
                    recipient="forma",
                    payload={"owner_user_id": "attacker"},
                    server_owned=True,
                    replace_existing=False,
                )

            job = store.get_job("job_atomic")
        finally:
            store.close()

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual("msg_owner", job["message_id"])
        self.assertEqual("owner", job["payload"]["owner_user_id"])


if __name__ == "__main__":
    unittest.main()
