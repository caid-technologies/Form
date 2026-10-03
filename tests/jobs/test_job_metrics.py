from __future__ import annotations

import unittest
from datetime import datetime, timezone

from forma_core.jobs.metrics import summarize_job_metrics
from forma_core.jobs.store import JobMetadataStore


class JobMetricsTests(unittest.TestCase):
    def test_sqlite_job_store_reports_persisted_metrics(self) -> None:
        store = JobMetadataStore(":memory:", backend="sqlite")
        try:
            for job_id in ("job_success", "job_partial", "job_failure"):
                store.create_job(
                    job_id=job_id,
                    message_id=f"message_{job_id}",
                    correlation_id=None,
                    action="forma.generate_project",
                    sender="frontend",
                    recipient="forma",
                    payload={"prompt": job_id},
                    server_owned=True,
                )
            store.mark_succeeded("job_success", {"project_ir": {}})
            store.mark_partial("job_partial", {"project_ir": {}})
            store.mark_failed("job_failure", "provider failed")

            metrics = store.get_metrics(days=7, hours=24)
        finally:
            store.close()

        self.assertEqual(3, metrics["jobs_today"])
        self.assertEqual(3, metrics["jobs_last_hour"])
        self.assertEqual(3, metrics["completed_jobs"])
        self.assertEqual(1, metrics["failed_jobs"])
        self.assertEqual(1, metrics["partial_jobs"])
        self.assertEqual(33.3, metrics["failure_rate"])

    def test_sqlite_job_store_merges_durable_worker_plan_rows(self) -> None:
        store = JobMetadataStore(":memory:", backend="sqlite")
        try:
            store.create_job(
                job_id="a2a_success",
                message_id="message_success",
                correlation_id=None,
                action="forma.generate_project",
                sender="frontend",
                recipient="forma",
                payload={"prompt": "success"},
                server_owned=True,
            )
            store.mark_succeeded("a2a_success", {"project_ir": {}})
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            metrics = store.get_metrics(
                days=7,
                hours=24,
                additional_rows=[
                    {"job_id": "generation-failure", "created_at": now, "status": "failed"},
                    {"job_id": "a2a_success", "created_at": now, "status": "failed"},
                ],
            )
        finally:
            store.close()

        self.assertEqual(2, metrics["jobs_today"])
        self.assertEqual(2, metrics["completed_jobs"])
        self.assertEqual(1, metrics["failed_jobs"])
        self.assertEqual(50.0, metrics["failure_rate"])

    def test_summarizes_daily_hourly_and_failure_metrics(self) -> None:
        now = datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc)
        rows = [
            {"created_at": "2026-08-09T12:15:00Z", "status": "succeeded"},
            {"created_at": "2026-08-09T11:45:00Z", "status": "failed"},
            {"created_at": "2026-08-08T10:00:00Z", "status": "failed"},
            {"created_at": "2026-08-03T09:00:00Z", "status": "completed"},
            {"created_at": "2026-08-01T09:00:00Z", "status": "failed"},
            {"created_at": "2026-08-09T09:00:00Z", "status": "queued"},
            {"created_at": "2026-08-09T10:00:00Z", "status": "cancelled"},
            {"created_at": "not-a-time", "status": "failed"},
            {"created_at": "2026-08-09T13:00:00Z", "status": "failed"},
        ]

        metrics = summarize_job_metrics(rows, now=now, days=7, hours=24)

        self.assertEqual(6, metrics["total_jobs"])
        self.assertEqual(4, metrics["jobs_today"])
        self.assertEqual(2, metrics["jobs_last_hour"])
        self.assertEqual(4, metrics["completed_jobs"])
        self.assertEqual(2, metrics["failed_jobs"])
        self.assertEqual(50.0, metrics["failure_rate"])
        self.assertEqual(1, metrics["daily"][0]["count"])
        self.assertEqual(4, metrics["daily"][-1]["count"])
        self.assertEqual(4, sum(bucket["count"] for bucket in metrics["hourly"]))
        self.assertEqual("2026-08-09T12:00:00Z", metrics["hourly"][-1]["period"])

    def test_empty_metrics_have_zero_failure_rate_and_complete_buckets(self) -> None:
        metrics = summarize_job_metrics(
            [],
            now=datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc),
            days=3,
            hours=4,
        )

        self.assertEqual(0.0, metrics["failure_rate"])
        self.assertEqual(3, len(metrics["daily"]))
        self.assertEqual(4, len(metrics["hourly"]))
        self.assertTrue(all(bucket["count"] == 0 for bucket in metrics["daily"] + metrics["hourly"]))

    def test_failure_metrics_use_the_selected_rolling_interval(self) -> None:
        metrics = summarize_job_metrics(
            [
                {"created_at": "2026-08-09T12:15:00Z", "status": "failed"},
                {"created_at": "2026-08-09T10:00:00Z", "status": "succeeded"},
            ],
            now=datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc),
            days=1,
            hours=1,
            interval_hours=1,
        )

        self.assertEqual(1, metrics["interval_hours"])
        self.assertEqual(1, metrics["total_jobs"])
        self.assertEqual(1, metrics["completed_jobs"])
        self.assertEqual(1, metrics["failed_jobs"])
        self.assertEqual(100.0, metrics["failure_rate"])

    def test_metric_windows_are_bounded(self) -> None:
        metrics = summarize_job_metrics([], days=100, hours=1000)

        self.assertEqual(31, metrics["window_days"])
        self.assertEqual(168, metrics["window_hours"])


if __name__ == "__main__":
    unittest.main()
