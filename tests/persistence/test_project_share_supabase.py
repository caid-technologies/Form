"""Exercise hosted queries through the actual PostgREST client and HTTP transport."""
import json
import unittest
from uuid import uuid4

import httpx
from postgrest import SyncPostgrestClient
from forma_core.persistence.repositories import SupabaseRepository


class ProjectShareSupabaseTests(unittest.TestCase):
    def test_hosted_queries_scope_capabilities_and_management(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=[])

        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            repository = SupabaseRepository(SyncPostgrestClient("https://database.test/rest/v1", http_client=http))
            record = {"id": str(uuid4()), "project_id": "project", "owner_user_id": "owner", "revision_id": "revision",
                      "token_hash": "a" * 64, "created_at": "2026-09-24T00:00:00Z", "revoked_at": None}
            repository.insert_project_share(record)
            self.assertEqual(json.loads(requests[-1].content), record)
            self.assertEqual(requests[-1].method, "POST")
            self.assertIsNone(repository.get_active_project_share("project", "owner", "revision", "a" * 64))
            lookup = requests[-1]
            self.assertEqual(lookup.url.params["token_hash"], "eq." + "a" * 64)
            self.assertEqual(lookup.url.params["revoked_at"], "is.null")
            repository.list_project_shares("project", "owner", "revision", limit=51, offset=50)
            listing = requests[-1]
            self.assertNotIn("token", listing.url.params["select"])
            self.assertEqual(listing.url.params["offset"], "50")
            self.assertEqual(listing.url.params["limit"], "51")
            self.assertEqual(listing.url.params["order"], "created_at.desc,id.desc")
            repository.revoke_project_share("project", "owner", "revision", record["id"], "2026-09-24T00:01:00Z")
            revocation = requests[-1]
            self.assertEqual(revocation.method, "PATCH")
            self.assertEqual(revocation.url.params["id"], "eq." + record["id"])
            self.assertEqual(revocation.url.params["revoked_at"], "is.null")
            self.assertEqual(json.loads(revocation.content), {"revoked_at": "2026-09-24T00:01:00Z"})
            for request in (lookup, listing, revocation):
                self.assertEqual(request.url.path, "/rest/v1/project_shares")
                for key, value in (("project_id", "project"), ("owner_user_id", "owner"), ("revision_id", "revision")):
                    self.assertEqual(request.url.params[key], f"eq.{value}")
