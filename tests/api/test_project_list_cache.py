from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from forma_core import project_list_cache


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, int]] = []

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.set_calls.append((key, ex))

    def incr(self, key: str) -> int:
        next_value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(next_value)
        return next_value


class ProjectListCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        project_list_cache._disabled_until = 0.0
        project_list_cache._client = None
        project_list_cache._client_identity = None

    def test_round_trip_uses_hashed_identity_and_configured_ttl(self) -> None:
        redis = _FakeRedis()
        projects = [{"project_id": "project-1", "can_chat": True}]

        with patch.object(project_list_cache, "_get_redis_client", return_value=redis), patch.dict(
            "os.environ",
            {"PROJECTS_CACHE_TTL_SECONDS": "90", "REDIS_CACHE_PREFIX": "test"},
            clear=False,
        ):
            missed, generation = project_list_cache.get_cached_project_list("mine", "user-secret-id")
            project_list_cache.cache_project_list("mine", "user-secret-id", projects, generation)
            cached, cached_generation = project_list_cache.get_cached_project_list("mine", "user-secret-id")

        self.assertIsNone(missed)
        self.assertEqual("0", generation)
        self.assertEqual(projects, cached)
        self.assertEqual("0", cached_generation)
        self.assertEqual(90, redis.set_calls[0][1])
        self.assertNotIn("user-secret-id", redis.set_calls[0][0])
        self.assertEqual(projects, json.loads(redis.values[redis.set_calls[0][0]]))

    def test_invalidation_moves_readers_to_a_new_generation(self) -> None:
        redis = _FakeRedis()
        projects = [{"project_id": "project-1"}]

        with patch.object(project_list_cache, "_get_redis_client", return_value=redis):
            _, old_generation = project_list_cache.get_cached_project_list("public", None)
            project_list_cache.cache_project_list("public", None, projects, old_generation)
            project_list_cache.invalidate_project_lists()
            cached, new_generation = project_list_cache.get_cached_project_list("public", None)

        self.assertIsNone(cached)
        self.assertEqual("0", old_generation)
        self.assertEqual("1", new_generation)

    def test_no_redis_is_a_no_op(self) -> None:
        with patch.object(project_list_cache, "_get_redis_client", return_value=None):
            cached, generation = project_list_cache.get_cached_project_list("public", None)
            project_list_cache.cache_project_list("public", None, [], generation)
            project_list_cache.invalidate_project_lists()

        self.assertIsNone(cached)
        self.assertIsNone(generation)

    def test_development_mode_allows_missing_redis_config(self) -> None:
        with patch.dict("os.environ", {"FORMA_DEV_MODE": "true"}, clear=True):
            project_list_cache.require_project_list_cache_config()

    def test_non_development_mode_requires_url_and_prefix(self) -> None:
        with patch.dict("os.environ", {"FORMA_DEV_MODE": "false"}, clear=True):
            with self.assertLogs(project_list_cache.logger, level="CRITICAL"):
                with self.assertRaisesRegex(RuntimeError, "UPSTASH_REDIS_REST_URL"):
                    project_list_cache.require_project_list_cache_config()

    def test_non_development_mode_accepts_complete_redis_config(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FORMA_DEV_MODE": "false",
                "REDIS_URL": "rediss://default:secret@example.test:6379/0",
                "REDIS_CACHE_PREFIX": "forma-preview",
            },
            clear=True,
        ):
            project_list_cache.require_project_list_cache_config()

    def test_non_development_mode_accepts_complete_upstash_config(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FORMA_DEV_MODE": "false",
                "UPSTASH_REDIS_REST_URL": "https://example.upstash.io",
                "UPSTASH_REDIS_REST_TOKEN": "secret",
                "REDIS_CACHE_PREFIX": "forma-production",
            },
            clear=True,
        ):
            project_list_cache.require_project_list_cache_config()

    def test_upstash_rest_client_sends_redis_commands(self) -> None:
        response = Mock()
        response.read.return_value = b'{"result":"cached-value"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        client = project_list_cache._UpstashRedisRestClient(
            "https://example.upstash.io/", "secret-token", 0.5
        )
        with patch.object(project_list_cache.request, "urlopen", return_value=response) as urlopen:
            result = client.get("cache-key")

        self.assertEqual("cached-value", result)
        redis_request = urlopen.call_args.args[0]
        self.assertEqual("https://example.upstash.io", redis_request.full_url)
        self.assertEqual(["GET", "cache-key"], json.loads(redis_request.data))
        self.assertEqual("Bearer secret-token", redis_request.get_header("Authorization"))
        self.assertEqual(0.5, urlopen.call_args.kwargs["timeout"])


class ProjectPageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        project_list_cache._disabled_until = 0.0
        self.redis = _FakeRedis()
        self.client_patch = patch.object(project_list_cache, "_get_redis_client", return_value=self.redis)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(setattr, project_list_cache, "_disabled_until", 0.0)

    def test_page_round_trip_uses_ttl_and_separates_all_query_dimensions(self) -> None:
        items = [{"project_id": "one"}]
        with patch.object(project_list_cache, "_cache_ttl_seconds", return_value=90):
            missed, generation = project_list_cache.get_cached_project_page("public", None, limit=6, offset=0, search="private query")
            self.assertIsNone(missed)
            project_list_cache.cache_project_page("public", None, items, 25, generation, limit=6, offset=0, search="private query")
        cached, _ = project_list_cache.get_cached_project_page("public", None, limit=6, offset=0, search="private query")
        self.assertEqual({"items": items, "total": 25}, cached)
        self.assertEqual(90, self.redis.set_calls[0][1])
        self.assertNotIn("private query", self.redis.set_calls[0][0])
        for scope, owner, limit, offset, search in [
            ("public", None, 6, 6, "private query"),
            ("public", None, 12, 0, "private query"),
            ("public", None, 6, 0, "different"),
            ("mine", "user-a", 6, 0, "private query"),
            ("public", "user-b", 6, 0, "private query"),
        ]:
            self.assertIsNone(project_list_cache.get_cached_project_page(scope, owner, limit=limit, offset=offset, search=search)[0])
        self.assertIsNone(project_list_cache.get_cached_project_list("public", None)[0])

    def test_invalidation_also_rejects_a_late_write_from_the_old_generation(self) -> None:
        _, generation = project_list_cache.get_cached_project_page("public", None, limit=6, offset=0)
        project_list_cache.invalidate_project_lists()
        project_list_cache.cache_project_page("public", None, [{"project_id": "deleted"}], 1, generation, limit=6, offset=0)
        cached, next_generation = project_list_cache.get_cached_project_page("public", None, limit=6, offset=0)
        self.assertIsNone(cached)
        self.assertEqual("1", next_generation)

    def test_empty_page_is_a_hit_and_does_not_mutate_the_stored_payload(self) -> None:
        _, generation = project_list_cache.get_cached_project_page("public", None, limit=6, offset=0)
        project_list_cache.cache_project_page("public", None, [], 0, generation, limit=6, offset=0)
        page, _ = project_list_cache.get_cached_project_page("public", None, limit=6, offset=0)
        self.assertEqual({"items": [], "total": 0}, page)
        page["items"].append({"project_id": "mutated"})
        self.assertEqual([], project_list_cache.get_cached_project_page("public", None, limit=6, offset=0)[0]["items"])

    def test_disabled_cache_and_failed_reads_fall_back_without_failing_requests(self) -> None:
        with patch.object(project_list_cache, "_get_redis_client", return_value=None):
            self.assertEqual((None, None), project_list_cache.get_cached_project_page("public", None, limit=6, offset=0))
            project_list_cache.cache_project_page("public", None, [], 0, None, limit=6, offset=0)
        with patch.object(self.redis, "get", side_effect=RuntimeError("offline")), self.assertLogs(project_list_cache.logger):
            self.assertEqual((None, None), project_list_cache.get_cached_project_page("public", None, limit=6, offset=0))
        self.assertEqual([], self.redis.set_calls)

    def test_malformed_payload_is_a_miss(self) -> None:
        key = project_list_cache._page_data_key("public", None, "0", 6, 0, None)
        for payload in [[], {"items": [], "total": True}, {"items": ["bad"], "total": 1}, {"items": [], "total": -1}]:
            project_list_cache._disabled_until = 0.0
            self.redis.values[key] = json.dumps(payload)
            with self.assertLogs(project_list_cache.logger):
                self.assertEqual((None, None), project_list_cache.get_cached_project_page("public", None, limit=6, offset=0))


if __name__ == "__main__":
    unittest.main()
