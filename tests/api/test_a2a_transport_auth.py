from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from apps.api import a2a, main
from apps.api.a2a import A2AAgentRegistration, A2AEvent, A2AHub
from apps.api.auth import (
    UserContext,
    require_a2a_user_context,
    require_mcp_user_context,
)


def request_with_authorization(value: str | None = None) -> Request:
    headers = [(b"authorization", value.encode("utf-8"))] if value else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
        }
    )


def user_context(user_id: str, *, admin: bool = False) -> UserContext:
    return UserContext(
        provider="clerk",
        subject=user_id,
        owner_user_id=user_id,
        is_authenticated=True,
        is_admin=admin,
    )


class FakeStreamWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, _name: str):
        return None


class A2ATransportAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_hub_rejects_cross_principal_registration_and_polling(self) -> None:
        hub = A2AHub()
        await hub.register("owned-agent", principal="user:owner", owner_user_id="owner")

        self.assertEqual(
            [],
            await hub.poll(
                "owned-agent",
                timeout=0,
                principal="user:owner",
                create_if_missing=False,
            ),
        )
        event = A2AEvent(action="test.event", recipient="owned-agent")
        await hub.publish(event, principal="user:owner", owner_user_id="owner")
        self.assertEqual([event], await hub.poll("owned-agent", timeout=0, principal="user:owner"))
        with self.assertRaises(PermissionError):
            await hub.register("owned-agent", principal="user:attacker", owner_user_id="attacker")
        with self.assertRaises(PermissionError):
            await hub.poll(
                "owned-agent",
                timeout=0,
                principal="user:attacker",
                create_if_missing=False,
            )

    async def test_hosted_http_transports_reject_anonymous_callers(self) -> None:
        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "hosted", "FORMA_AUTH_MODE": "clerk"},
            clear=True,
        ):
            with self.assertRaises(HTTPException) as register_error:
                await require_a2a_user_context(request_with_authorization())
            registration_response = TestClient(main.app).put(
                "/a2a/agents/anonymous-agent",
                json={"name": "Anonymous"},
            )
            poll_response = TestClient(main.app).get("/a2a/agents/anonymous-agent/events?timeout=0&limit=1")
            capabilities_response = TestClient(main.app).get("/a2a/capabilities")

        self.assertEqual(401, register_error.exception.status_code)
        self.assertEqual(401, registration_response.status_code)
        self.assertEqual(401, poll_response.status_code)
        self.assertEqual(401, capabilities_response.status_code)

    async def test_local_auth_is_not_used_for_hosted_a2a(self) -> None:
        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "hosted", "FORMA_AUTH_MODE": "local"},
            clear=True,
        ):
            with self.assertRaises(HTTPException) as raised:
                await require_a2a_user_context(request_with_authorization())

        self.assertEqual(401, raised.exception.status_code)

    async def test_local_a2a_requires_explicit_local_deployment_mode(self) -> None:
        with patch.dict(os.environ, {"FORMA_AUTH_MODE": "local"}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                await require_a2a_user_context(request_with_authorization())

        self.assertEqual(401, raised.exception.status_code)

        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "local", "FORMA_AUTH_MODE": "local"},
            clear=True,
        ):
            context = await require_a2a_user_context(request_with_authorization())

        self.assertEqual("local", context.provider)
        self.assertTrue(context.is_authenticated)

    async def test_local_mcp_requires_explicit_local_deployment_mode(self) -> None:
        with patch.dict(os.environ, {"FORMA_AUTH_MODE": "local"}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                await require_mcp_user_context(request_with_authorization())

        self.assertEqual(401, raised.exception.status_code)

    async def test_hosted_websocket_rejects_anonymous_connection(self) -> None:
        with patch.dict(
            os.environ,
            {"FORMA_DEPLOYMENT_MODE": "hosted", "FORMA_AUTH_MODE": "clerk"},
            clear=True,
        ):
            with self.assertRaises(WebSocketDisconnect) as raised:
                with TestClient(main.app).websocket_connect("/a2a/socket/anonymous-agent"):
                    pass

        self.assertEqual(4401, raised.exception.code)

    async def test_same_user_can_register_and_poll_but_other_user_cannot(self) -> None:
        agent_id = "auth-test-agent"
        owner = user_context("owner-user")
        attacker = user_context("attacker-user")
        with patch.dict(os.environ, {"FORMA_AUTH_MODE": "clerk"}, clear=True):
            await main.register_a2a_agent(agent_id, A2AAgentRegistration(name="Owner"), owner)
            self.assertEqual([], await main.poll_a2a_events(agent_id, timeout=0, limit=1, user=owner))
            with self.assertRaises(HTTPException) as raised:
                await main.poll_a2a_events(agent_id, timeout=0, limit=1, user=attacker)

        self.assertEqual(403, raised.exception.status_code)

    async def test_mcp_polling_cannot_cross_user_queue_ownership(self) -> None:
        hub = A2AHub()
        owner = user_context("mcp-owner")
        attacker = user_context("mcp-attacker")
        principal = a2a.a2a_principal_for_user(owner)
        await hub.register("mcp-owned-agent", principal=principal, owner_user_id="mcp-owner")
        await hub.publish(
            A2AEvent(action="test.event", recipient="mcp-owned-agent"),
            principal=principal,
            owner_user_id="mcp-owner",
        )

        with patch.object(a2a, "A2A_HUB", hub):
            result = await a2a._call_mcp_tool(
                "forma.a2a.poll_events",
                {"agent_id": "mcp-owned-agent", "timeout": 0, "limit": 1},
                owner,
            )
            with self.assertRaises(PermissionError):
                await a2a._call_mcp_tool(
                    "forma.a2a.poll_events",
                    {"agent_id": "mcp-owned-agent", "timeout": 0, "limit": 1},
                    attacker,
                )

        self.assertEqual(1, len(result["events"]))

    async def test_job_id_collision_cannot_replace_another_principal_job(self) -> None:
        message = a2a.A2AMessage(
            job_id="shared-job-id",
            message_id="attacker-message",
            sender="attacker-agent",
            action="forma.debug_config",
        )
        existing = {
            "job_id": "shared-job-id",
            "message_id": "owner-message",
            "action": "forma.debug_config",
            "sender": "owner-agent",
            "recipient": "forma",
            "payload": {"owner_user_id": "owner-user"},
        }
        with patch.object(a2a.JOB_STORE, "get_job", return_value=existing), patch.object(
            a2a.A2A_HUB, "register", new=AsyncMock()
        ), patch.object(a2a.JOB_STORE, "create_job") as create_job:
            with self.assertRaises(PermissionError):
                await a2a.submit_a2a_message(message, user_context("attacker-user"))

        create_job.assert_not_called()

    async def test_a2a_submission_uses_non_replacing_job_insert(self) -> None:
        message = a2a.A2AMessage(
            job_id="atomic-job",
            message_id="atomic-message",
            sender="atomic-sender",
            recipient="atomic-recipient",
            action="custom.action",
        )
        with patch.object(a2a.A2A_HUB, "register", new=AsyncMock()), patch.object(
            a2a.A2A_HUB, "authorize", new=AsyncMock()
        ), patch.object(a2a.A2A_HUB, "publish", new=AsyncMock()), patch.object(
            a2a.JOB_STORE, "get_job", return_value=None
        ), patch.object(
            a2a.JOB_STORE, "create_job", return_value={"job_id": "atomic-job"}
        ) as create_job, patch.object(a2a.JOB_STORE, "mark_routed"):
            await a2a.submit_a2a_message(message, user_context("atomic-user"))

        self.assertFalse(create_job.call_args.kwargs["replace_existing"])

    async def test_a2a_service_credential_resolves_without_local_fallback(self) -> None:
        key = "a" * 32
        with patch.dict(
            os.environ,
            {
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_AUTH_MODE": "local",
                "FORMA_A2A_API_KEY": key,
            },
            clear=True,
        ):
            context = await require_a2a_user_context(request_with_authorization(f"Bearer {key}"))

        self.assertEqual("a2a-api-key", context.provider)
        self.assertIsNone(context.owner_user_id)
        self.assertTrue(context.is_authenticated)

    def test_a2a_service_credential_owns_rest_queue_and_mcp_alias(self) -> None:
        key = "s" * 32
        agent_id = "service-route-agent"
        with patch.dict(
            os.environ,
            {
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_AUTH_MODE": "local",
                "FORMA_A2A_API_KEY": key,
            },
            clear=True,
        ):
            client = TestClient(main.app)
            headers = {"Authorization": f"Bearer {key}"}
            registration = client.put(
                f"/a2a/agents/{agent_id}",
                headers=headers,
                json={"name": "Service agent"},
            )
            polling = client.get(
                f"/a2a/agents/{agent_id}/events?timeout=0&limit=1",
                headers=headers,
            )
            mcp = client.post(
                "/a2a/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": "ping", "method": "ping"},
            )

        self.assertEqual(200, registration.status_code)
        self.assertEqual(200, polling.status_code)
        self.assertEqual([], polling.json())
        self.assertEqual(200, mcp.status_code)
        self.assertEqual({}, mcp.json()["result"])

    async def test_service_principal_can_only_read_its_own_mcp_job(self) -> None:
        service = UserContext(
            provider="a2a-api-key",
            subject="a2a-service",
            owner_user_id=None,
            is_authenticated=True,
            is_admin=False,
        )
        own_job = {
            "job_id": "service-job",
            "payload": {"_forma_a2a_principal": a2a.a2a_principal_for_user(service)},
        }
        other_job = {
            "job_id": "other-job",
            "payload": {"_forma_a2a_principal": "principal:other"},
        }
        with patch.object(a2a.JOB_STORE, "get_job", return_value=own_job):
            self.assertEqual(
                own_job,
                await a2a._call_mcp_tool("forma.a2a.get_job", {"job_id": "service-job"}, service),
            )
        with patch.object(a2a.JOB_STORE, "get_job", return_value=other_job):
            with self.assertRaises(PermissionError):
                await a2a._call_mcp_tool("forma.a2a.get_job", {"job_id": "other-job"}, service)
        with patch.object(a2a.JOB_STORE, "list_jobs", return_value=[own_job, other_job]) as list_jobs:
            result = await a2a._call_mcp_tool("forma.a2a.list_jobs", {}, service)
        self.assertEqual([own_job], result["jobs"])
        list_jobs.assert_called_once_with(sender=None, status=None, limit=200)

    def test_service_principal_can_get_and_cancel_its_own_rest_job(self) -> None:
        service = UserContext(
            provider="a2a-api-key",
            subject="a2a-service",
            owner_user_id=None,
            is_authenticated=True,
            is_admin=False,
        )
        own_job = {
            "job_id": "service-rest-job",
            "status": "running",
            "payload": {"_forma_a2a_principal": a2a.a2a_principal_for_user(service)},
        }
        cancelled_job = {**own_job, "status": "cancelled"}
        with patch.object(main.JOB_STORE, "get_job", return_value=own_job):
            self.assertEqual(own_job, main.get_a2a_job("service-rest-job", service))
        with patch.object(main.JOB_STORE, "get_job", return_value=own_job), patch.object(
            main.JOB_STORE, "mark_cancelled", return_value=cancelled_job
        ), patch.object(main, "_delete_cancelled_generation_projects") as delete_projects:
            self.assertEqual(cancelled_job, main.cancel_a2a_job("service-rest-job", service))
        delete_projects.assert_called_once_with("service-rest-job", cancelled_job)

    async def test_websocket_route_passes_authenticated_context_to_handler(self) -> None:
        websocket = SimpleNamespace()
        context = user_context("socket-user")
        with patch.object(main, "require_a2a_websocket_context", new=AsyncMock(return_value=context)), patch.object(
            main, "handle_a2a_websocket", new=AsyncMock()
        ) as handler:
            await main.a2a_websocket_endpoint(websocket, "socket-agent")

        handler.assert_awaited_once_with(websocket, "socket-agent", context)

    async def test_websocket_mcp_receives_authenticated_context(self) -> None:
        context = user_context("socket-user", admin=True)
        websocket = SimpleNamespace(
            accept=AsyncMock(),
            close=AsyncMock(),
            receive_json=AsyncMock(
                side_effect=[
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    WebSocketDisconnect(),
                ]
            ),
            send_json=AsyncMock(),
        )
        with patch.object(a2a.A2A_HUB, "register", new=AsyncMock()), patch.object(
            a2a.A2A_HUB, "publish", new=AsyncMock()
        ), patch.object(a2a, "_websocket_sender", new=AsyncMock()), patch.object(
            a2a, "handle_mcp_json_rpc", new=AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        ) as handle_mcp, patch.object(a2a, "get_a2a_capabilities", return_value={}):
            await a2a.handle_a2a_websocket(websocket, "socket-agent", context)

        handle_mcp.assert_awaited_once_with(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            context,
        )

    async def test_websocket_mcp_rejects_non_admin_user_context(self) -> None:
        context = user_context("socket-user")
        websocket = SimpleNamespace(
            accept=AsyncMock(),
            close=AsyncMock(),
            receive_json=AsyncMock(
                side_effect=[
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    WebSocketDisconnect(),
                ]
            ),
            send_json=AsyncMock(),
        )
        with patch.object(a2a.A2A_HUB, "register", new=AsyncMock()), patch.object(
            a2a.A2A_HUB, "publish", new=AsyncMock()
        ), patch.object(a2a, "_websocket_sender", new=AsyncMock()), patch.object(
            a2a, "handle_mcp_json_rpc", new=AsyncMock()
        ) as handle_mcp, patch.object(a2a, "get_a2a_capabilities", return_value={}):
            await a2a.handle_a2a_websocket(websocket, "socket-agent", context)

        handle_mcp.assert_not_awaited()
        error = websocket.send_json.await_args.args[0]
        self.assertEqual("authorization_required", error["error"]["data"]["code"])

    async def test_websocket_rejects_cross_principal_agent_connection(self) -> None:
        owner = user_context("socket-owner")
        attacker = user_context("socket-attacker")
        hub = A2AHub()

        def websocket() -> SimpleNamespace:
            return SimpleNamespace(
                accept=AsyncMock(),
                close=AsyncMock(),
                receive_json=AsyncMock(side_effect=[WebSocketDisconnect()]),
                send_json=AsyncMock(),
            )

        with patch.object(a2a, "A2A_HUB", hub), patch.object(a2a, "_websocket_sender", new=AsyncMock()), patch.object(
            a2a, "get_a2a_capabilities", return_value={}
        ):
            owner_socket = websocket()
            await a2a.handle_a2a_websocket(owner_socket, "shared-socket-agent", owner)
            attacker_socket = websocket()
            await a2a.handle_a2a_websocket(attacker_socket, "shared-socket-agent", attacker)

        owner_socket.accept.assert_awaited_once()
        attacker_socket.accept.assert_not_awaited()
        attacker_socket.close.assert_awaited_once_with(code=4403, reason="The agent is owned by another principal.")

    async def test_hosted_tcp_is_disabled_without_a_service_credential(self) -> None:
        a2a._tcp_server = None
        with patch.dict(
            os.environ,
            {
                "A2A_SOCKET_ENABLED": "true",
                "A2A_SOCKET_HOST": "0.0.0.0",
                "A2A_SOCKET_PORT": "8766",
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_AUTH_MODE": "clerk",
            },
            clear=True,
        ), patch.object(a2a.asyncio, "start_server", new=AsyncMock()) as start_server:
            result = await a2a.start_a2a_tcp_server()

        self.assertIsNone(result)
        start_server.assert_not_awaited()

    async def test_hosted_tcp_requires_and_accepts_service_handshake(self) -> None:
        key = "b" * 32
        reader = asyncio.StreamReader()
        reader.feed_data(
            (json.dumps({"type": "auth", "token": f"Bearer {key}", "agent_id": "tcp-agent"}) + "\n").encode()
        )
        reader.feed_eof()
        writer = FakeStreamWriter()
        with patch.dict(
            os.environ,
            {
                "A2A_SOCKET_HOST": "0.0.0.0",
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_AUTH_MODE": "clerk",
                "FORMA_A2A_API_KEY": key,
            },
            clear=True,
        ):
            with patch.object(a2a, "_tcp_auth_policy", None):
                result = await a2a._authenticate_tcp_client(reader, writer)

        self.assertIsNotNone(result)
        agent_id, context = result
        self.assertEqual("tcp-agent", agent_id)
        self.assertEqual("a2a-api-key", context.provider)
        self.assertEqual([], writer.writes)

    async def test_hosted_tcp_handler_scopes_authenticated_agent_queue(self) -> None:
        key = "c" * 32
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps({"type": "auth", "token": key, "agent_id": "tcp-handler-agent"})
                + "\n"
                + json.dumps({"action": "a2a.ping"})
                + "\n"
            ).encode()
        )
        reader.feed_eof()
        writer = FakeStreamWriter()
        hub = A2AHub()
        with patch.dict(
            os.environ,
            {
                "A2A_SOCKET_HOST": "0.0.0.0",
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_AUTH_MODE": "clerk",
                "FORMA_A2A_API_KEY": key,
            },
            clear=True,
        ), patch.object(a2a, "_tcp_auth_policy", None), patch.object(
            a2a, "A2A_HUB", hub
        ), patch.object(a2a, "_tcp_sender", new=AsyncMock()), patch.object(
            a2a, "get_a2a_capabilities", return_value={}
        ), patch.object(a2a, "submit_a2a_message", new=AsyncMock()) as submit:
            await a2a._handle_tcp_client(reader, writer)

        submit.assert_awaited_once()
        message, context = submit.await_args.args
        self.assertEqual("tcp-handler-agent", message.sender)
        self.assertEqual("a2a-api-key", context.provider)
        events = await hub.poll(
            "tcp-handler-agent",
            timeout=0,
            principal=a2a.a2a_principal_for_user(context),
        )
        self.assertEqual(1, len(events))
        self.assertEqual("ready", events[0].type)

    async def test_hosted_tcp_rejects_anonymous_connection_without_handshake(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"sender":"anonymous"}\n')
        reader.feed_eof()
        writer = FakeStreamWriter()
        with patch.dict(
            os.environ,
            {
                "A2A_SOCKET_HOST": "0.0.0.0",
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_AUTH_MODE": "clerk",
            },
            clear=True,
        ):
            with patch.object(a2a, "_tcp_auth_policy", None):
                result = await a2a._authenticate_tcp_client(reader, writer)

        self.assertIsNone(result)
        self.assertEqual("a2a_authentication_required", json.loads(writer.writes[0])["error"]["code"])

    async def test_local_tcp_infers_sender_from_first_message_without_handshake(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"sender":"local-agent","action":"a2a.ping"}\n')
        reader.feed_eof()
        writer = FakeStreamWriter()
        local_hub = A2AHub()
        with patch.dict(
            os.environ,
            {
                "A2A_SOCKET_HOST": "127.0.0.1",
                "FORMA_DEPLOYMENT_MODE": "local",
                "FORMA_AUTH_MODE": "local",
            },
            clear=True,
        ), patch.object(a2a, "_tcp_auth_policy", None), patch.object(
            a2a, "A2A_HUB", local_hub
        ), patch.object(a2a, "_tcp_sender", new=AsyncMock()), patch.object(
            a2a, "get_a2a_capabilities", return_value={}
        ), patch.object(a2a, "submit_a2a_message", new=AsyncMock()) as submit:
            await a2a._handle_tcp_client(reader, writer)

        submit.assert_awaited_once()
        message, context = submit.await_args.args
        self.assertEqual("local-agent", message.sender)
        self.assertEqual("local", context.provider)
        events = await local_hub.poll("local-agent", timeout=0, principal=a2a.a2a_principal_for_user(context))
        self.assertEqual(1, len(events))
        self.assertEqual("ready", events[0].type)


if __name__ == "__main__":
    unittest.main()
