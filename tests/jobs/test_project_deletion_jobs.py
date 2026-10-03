from __future__ import annotations

import unittest
import uuid

from forma_core.jobs.store import JobMetadataStore


class ProjectDeletionJobTests(unittest.TestCase):
    def test_project_jobs_are_cancelled_and_deleted_without_touching_other_jobs(self) -> None:
        store = JobMetadataStore(db_path=":memory:")
        try:
            project_id = str(uuid.uuid4())
            other_project_id = str(uuid.uuid4())
            matching_ids = []
            for index, payload in enumerate(
                (
                    {"project_id": project_id},
                    {"source_project_id": project_id},
                    {"project_id": other_project_id},
                )
            ):
                job_id = f"job-{index}"
                store.create_job(
                    job_id=job_id,
                    message_id=f"message-{index}",
                    correlation_id=None,
                    action="forma.generate_project",
                    sender="test-user",
                    recipient="forma",
                    payload=payload,
                    server_owned=True,
                )
                if index < 2:
                    matching_ids.append(job_id)

            self.assertEqual(2, store.cancel_project_jobs(project_id))
            self.assertEqual(
                {"cancelled"},
                {store.get_job(job_id)["status"] for job_id in matching_ids},
            )
            self.assertEqual("queued", store.get_job("job-2")["status"])
            self.assertEqual(2, store.delete_project_jobs(project_id))
            self.assertTrue(all(store.get_job(job_id) is None for job_id in matching_ids))
            self.assertIsNotNone(store.get_job("job-2"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
