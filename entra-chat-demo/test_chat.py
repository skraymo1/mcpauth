"""Offline tests for the chat loop, its MCP and model clients, and the chat routes; no network calls."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
import requests

import chat
import config
import mcp_server
import webapp

try:
    from azure.core.credentials import AccessToken
    from azure.identity import CredentialUnavailableError
except ImportError:  # azure-identity is needed only when a model endpoint is configured
    AccessToken = None

USER = {"name": "Alice Rivera", "preferred_username": "alice@example.com"}
TENANT = "11111111-1111-4111-8111-111111111111"
ENDPOINT = "https://unit-test.openai.azure.com"


def tool_call(arguments, name="get_inventory", call_id="call_1"):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def wants_tools(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def says(text):
    return {"role": "assistant", "content": text}


class FakeMcp:
    """Behaves like McpSession talking to the demo MCP server."""

    def __init__(self, tools=None, list_error=None):
        self.tools = [mcp_server.TOOL] if tools is None else tools
        self.list_error = list_error
        self.calls = []
        self.renewed = False

    def list_tools(self):
        if self.list_error:
            raise self.list_error
        return self.tools

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if arguments.get("sku") != "WIDGET-001":
            return {"isError": True, "content": [{"type": "text", "text": "Unknown demo SKU; use WIDGET-001."}]}
        data = {"sku": "WIDGET-001", "quantity": 42, "warehouse": "ATL-DEMO",
                "requested_by": USER["preferred_username"]}
        return {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}


class FakeModel:
    label = "fake-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, tools, tool_choice=None):
        self.requests.append({"messages": json.loads(json.dumps(messages)), "tools": tools,
                              "tool_choice": tool_choice})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Reply:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError("empty body")
        return self.body


def rpc(result):
    return Reply(200, {"jsonrpc": "2.0", "id": 1, "result": result})


INITIALIZED = rpc({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}})


class TurnTests(unittest.TestCase):
    def turn(self, model, question="How many WIDGET-001 do we have?", history=(), mcp=None):
        self.mcp = mcp or FakeMcp()
        return chat.run_turn(question, list(history), user=USER, mcp=self.mcp, model=model)

    def test_tool_runs_as_the_user_and_its_result_goes_back_to_the_model(self):
        model = FakeModel(wants_tools(tool_call({"sku": "WIDGET-001"})), says("42 units at ATL-DEMO."))
        turn = self.turn(model)
        self.assertEqual((turn.answer, turn.engine, turn.error), ("42 units at ATL-DEMO.", "fake-model", False))
        self.assertEqual(self.mcp.calls, [("get_inventory", {"sku": "WIDGET-001"})])
        self.assertEqual(turn.trace[0]["result"]["requested_by"], "alice@example.com")
        first, second = model.requests
        self.assertIn("Alice Rivera (alice@example.com)", first["messages"][0]["content"])
        self.assertEqual([tool["function"]["name"] for tool in first["tools"]], ["get_inventory"])
        self.assertEqual(second["messages"][-2]["tool_calls"][0]["id"], "call_1")
        result = second["messages"][-1]
        self.assertEqual((result["role"], result["tool_call_id"]), ("tool", "call_1"))
        self.assertEqual(json.loads(result["content"])["quantity"], 42)

    def test_only_valid_read_only_tools_are_offered(self):
        tools = [mcp_server.TOOL, {"name": "delete_inventory", "annotations": {"readOnlyHint": False}},
                 {"name": "unannotated_tool"}, {"name": "bad name", "annotations": {"readOnlyHint": True}},
                 "not-a-tool"]
        self.assertEqual([tool["function"]["name"] for tool in chat.function_tools(tools)], ["get_inventory"])

    def test_tool_that_was_not_offered_is_not_executed(self):
        tools = [mcp_server.TOOL, {"name": "delete_inventory", "annotations": {"readOnlyHint": False}}]
        model = FakeModel(wants_tools(tool_call({"sku": "WIDGET-001"}, name="delete_inventory")), says("No."))
        turn = self.turn(model, mcp=FakeMcp(tools=tools))
        self.assertEqual(self.mcp.calls, [])
        self.assertFalse(turn.trace[0]["ok"])
        self.assertIn("read-only", json.loads(model.requests[1]["messages"][-1]["content"])["error"])

    def test_invalid_arguments_are_not_executed(self):
        for arguments in ("not json", "[1, 2]", "null"):
            with self.subTest(arguments=arguments):
                turn = self.turn(FakeModel(wants_tools(tool_call(arguments)), says("Sorry.")))
                self.assertEqual(self.mcp.calls, [])
                self.assertIn("invalid tool arguments", turn.trace[0]["detail"])

    def test_tool_rounds_are_bounded_then_the_model_must_answer(self):
        rounds = [wants_tools(tool_call({"sku": "WIDGET-001"}, call_id=f"call_{n}"))
                  for n in range(chat.MAX_TOOL_ROUNDS)]
        model = FakeModel(*rounds, says("Stopping here."))
        turn = self.turn(model)
        self.assertEqual(turn.answer, "Stopping here.")
        self.assertEqual(len(self.mcp.calls), chat.MAX_TOOL_ROUNDS)
        self.assertEqual([request["tool_choice"] for request in model.requests],
                         [None] * chat.MAX_TOOL_ROUNDS + ["none"])

    def test_tool_calls_per_round_are_capped_but_all_answered(self):
        count = chat.MAX_CALLS_PER_ROUND + 2
        calls = [tool_call({"sku": "WIDGET-001"}, call_id=f"call_{n}") for n in range(count)]
        model = FakeModel(wants_tools(*calls), says("Done."))
        self.turn(model)
        self.assertEqual(len(self.mcp.calls), chat.MAX_CALLS_PER_ROUND)
        answered = [m["tool_call_id"] for m in model.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(answered, [f"call_{n}" for n in range(count)])

    def test_access_denied_is_explained_without_calling_the_model(self):
        denied = chat.McpError(403, "access_denied", "User lacks the Inventory.Reader role")
        model = FakeModel()
        turn = self.turn(model, mcp=FakeMcp(list_error=denied))
        self.assertTrue(turn.error)
        self.assertIn("HTTP 403", turn.answer)
        self.assertIn("Inventory.Reader", turn.answer)
        self.assertEqual(model.requests, [])
        self.assertEqual(turn.trace, [{"tool": "tools/list", "ok": False, "status": 403,
                                       "detail": "User lacks the Inventory.Reader role"}])

    def test_other_mcp_failures_propagate(self):
        with self.assertRaises(chat.McpError):
            self.turn(FakeModel(), mcp=FakeMcp(list_error=chat.McpError(500, "server_error")))

    def test_history_is_sent_as_plain_messages(self):
        history = [{"role": "user", "content": "Earlier question"},
                   {"role": "assistant", "content": "Earlier answer", "engine": "x",
                    "trace": [{"tool": "get_inventory"}], "renewed": True, "error": False}]
        model = FakeModel(says("Next answer."))
        self.turn(model, question="Next question", history=history)
        self.assertEqual(model.requests[0]["messages"][1:], [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "user", "content": "Next question"},
        ])

    def test_empty_model_reply_gets_fallback_text(self):
        self.assertIn("no answer", self.turn(FakeModel(says("  "))).answer)

    def test_offline_responder_looks_up_each_sku(self):
        turn = self.turn(None, question="Compare widget-001 with TEST-404 using the AWS-hosted tool.")
        self.assertEqual(turn.engine, chat.OFFLINE_ENGINE)
        self.assertEqual(self.mcp.calls, [("get_inventory", {"sku": "WIDGET-001"}),
                                          ("get_inventory", {"sku": "TEST-404"})])
        self.assertEqual(turn.answer.splitlines(), ["WIDGET-001: 42 in stock at ATL-DEMO.",
                                                    "TEST-404: Unknown demo SKU; use WIDGET-001."])

    def test_offline_responder_without_a_sku_asks_for_one(self):
        turn = self.turn(None, question="hello there")
        self.assertEqual((self.mcp.calls, turn.trace), ([], []))
        self.assertIn("WIDGET-001", turn.answer)

    def test_find_skus(self):
        self.assertEqual(chat.find_skus("read-only AWS-hosted WIDGET-SPACE, gadget-42, WIDGET-SPACE, A-1, B-2"),
                         ["WIDGET-SPACE", "GADGET-42", "A-1"])


class McpSessionTests(unittest.TestCase):
    def setUp(self):
        self.token = "user-token-1"

    def provider(self, force):
        if force:
            self.token = "user-token-2"
        return self.token, force

    def session(self):
        return chat.McpSession("http://127.0.0.1:1/mcp", self.provider)

    def test_initializes_once_then_sends_protocol_header(self):
        session = self.session()
        replies = [INITIALIZED, Reply(202), rpc({"tools": [mcp_server.TOOL]}), rpc({"content": []})]
        with patch.object(chat.requests, "post", side_effect=replies) as post:
            self.assertEqual(session.list_tools(), [mcp_server.TOOL])
            self.assertEqual(session.call_tool("get_inventory", {"sku": "WIDGET-001"}), {"content": []})
        sent = [call.kwargs for call in post.call_args_list]
        self.assertEqual([kwargs["json"]["method"] for kwargs in sent],
                         ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertEqual(sent[3]["json"]["params"], {"name": "get_inventory", "arguments": {"sku": "WIDGET-001"}})
        self.assertTrue(all(kwargs["headers"]["Authorization"] == "Bearer user-token-1" for kwargs in sent))
        self.assertNotIn("MCP-Protocol-Version", sent[0]["headers"])
        self.assertEqual([kwargs["headers"].get("MCP-Protocol-Version") for kwargs in sent[1:]], ["2025-06-18"] * 3)
        self.assertTrue(all(kwargs["allow_redirects"] is False for kwargs in sent))
        self.assertFalse(session.renewed)

    def test_rejected_token_is_renewed_once_and_retried(self):
        session = self.session()
        expired = Reply(401, {"error": "invalid_token", "error_description": "ExpiredSignatureError"})
        with patch.object(chat.requests, "post", side_effect=[expired, INITIALIZED, Reply(202), rpc({"tools": []})]) as post:
            self.assertEqual(session.list_tools(), [])
        self.assertTrue(session.renewed)
        self.assertEqual([call.kwargs["headers"]["Authorization"] for call in post.call_args_list],
                         ["Bearer user-token-1"] + ["Bearer user-token-2"] * 3)

    def test_second_rejection_is_an_error(self):
        expired = Reply(401, {"error": "invalid_token", "error_description": "ExpiredSignatureError"})
        with patch.object(chat.requests, "post", side_effect=[expired, expired]) as post, \
                self.assertRaises(chat.McpError) as caught:
            self.session().list_tools()
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(post.call_count, 2)

    def test_failures_raise_mcp_errors(self):
        cases = {
            "denied": ([Reply(403, {"error": "access_denied", "error_description": "No role"})], 403, "access_denied"),
            "unreadable": ([Reply(502)], 502, None),
            "missing protocol": ([rpc({})], 502, "invalid_response"),
            "notification": ([INITIALIZED, Reply(400, {})], 400, "initialize_failed"),
            "json-rpc error": ([INITIALIZED, Reply(202), Reply(200, {"jsonrpc": "2.0", "id": 2, "error": {
                "code": -32601, "message": "Method not found"}})], 200, "jsonrpc_error"),
        }
        for name, (replies, status, error) in cases.items():
            with self.subTest(name), patch.object(chat.requests, "post", side_effect=replies):
                with self.assertRaises(chat.McpError) as caught:
                    self.session().list_tools()
                self.assertEqual((caught.exception.status, caught.exception.error), (status, error))


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        self.model = chat.AzureOpenAIChat(ENDPOINT, "gpt-5-mini", TENANT, "low")
        self.model._bearer = lambda renew=False: "app-token-2" if renew else "app-token-1"
        self.ok = Reply(200, {"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

    def test_request_shape(self):
        tools = chat.function_tools([mcp_server.TOOL])
        with patch.object(chat.requests, "post", return_value=self.ok) as post:
            self.assertEqual(self.model.complete([{"role": "user", "content": "q"}], tools)["content"], "hi")
            self.model.complete([{"role": "user", "content": "q"}], tools, tool_choice="none")
            self.model.complete([{"role": "user", "content": "q"}], [], tool_choice="none")
        first, forced, toolless = post.call_args_list
        self.assertEqual(self.model.label, "gpt-5-mini (Azure OpenAI)")
        self.assertEqual(first.args[0], ENDPOINT + "/openai/v1/chat/completions")
        self.assertEqual(first.kwargs["headers"], {"Authorization": "Bearer app-token-1"})
        self.assertFalse(first.kwargs["allow_redirects"])
        body = first.kwargs["json"]
        self.assertEqual((body["model"], body["reasoning_effort"], body["tools"]), ("gpt-5-mini", "low", tools))
        self.assertNotIn("temperature", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(forced.kwargs["json"]["tool_choice"], "none")
        self.assertFalse({"tools", "tool_choice"} & set(toolless.kwargs["json"]))

    def test_rejected_app_token_is_renewed_once(self):
        with patch.object(chat.requests, "post", side_effect=[Reply(401, {}), self.ok]) as post:
            self.model.complete([], [])
        self.assertEqual([call.kwargs["headers"]["Authorization"] for call in post.call_args_list],
                         ["Bearer app-token-1", "Bearer app-token-2"])

    def test_failures_become_actionable_messages(self):
        for status, text in ((401, "az login"), (403, "Foundry User"), (404, "AZURE_OPENAI_DEPLOYMENT"),
                             (429, "rate limited"), (500, "HTTP 500")):
            with self.subTest(status=status):
                with patch.object(chat.requests, "post", return_value=Reply(status, {"error": {"code": "x"}})), \
                        self.assertRaises(chat.ModelUnavailable) as caught:
                    self.model.complete([], [])
                self.assertIn(text, str(caught.exception))

    def test_network_and_format_failures(self):
        with patch.object(chat.requests, "post", side_effect=requests.ConnectionError("down")), \
                self.assertRaises(chat.ModelUnavailable):
            self.model.complete([], [])
        for body in ({"choices": []}, {"choices": [{"message": "text"}]}, None):
            with self.subTest(body=body), patch.object(chat.requests, "post", return_value=Reply(200, body)), \
                    self.assertRaises(chat.ModelUnavailable):
                self.model.complete([], [])


@unittest.skipIf(AccessToken is None, "azure-identity is not installed")
class AppCredentialTests(unittest.TestCase):
    def test_cli_token_is_cached_until_near_expiry(self):
        model = chat.AzureOpenAIChat(ENDPOINT, "gpt-5-mini", TENANT, "low")
        now = int(time.time())
        with patch("azure.identity.AzureCliCredential") as credential:
            credential.return_value.get_token.side_effect = [
                AccessToken("t1", now + 60), AccessToken("t2", now + 3600), AccessToken("t3", now + 3600)]
            tokens = [model._bearer(), model._bearer(), model._bearer(), model._bearer(renew=True)]
        self.assertEqual(tokens, ["t1", "t2", "t2", "t3"])
        credential.assert_called_once_with(tenant_id=TENANT, process_timeout=30)
        credential.return_value.get_token.assert_called_with(chat.MODEL_SCOPE)

    def test_missing_cli_login_is_reported(self):
        model = chat.AzureOpenAIChat(ENDPOINT, "gpt-5-mini", TENANT, "low")
        with patch("azure.identity.AzureCliCredential") as credential:
            credential.return_value.get_token.side_effect = CredentialUnavailableError("Please run 'az login'")
            with self.assertRaises(chat.ModelUnavailable) as caught:
                model._bearer()
        self.assertIn("az login", str(caught.exception))

    def test_managed_identity_replaces_the_cli_login_when_configured(self):
        identity = "22222222-2222-4222-8222-222222222222"
        model = chat.AzureOpenAIChat(ENDPOINT, "gpt-5-mini", TENANT, "low", identity)
        with patch("azure.identity.ManagedIdentityCredential") as managed, \
                patch("azure.identity.AzureCliCredential") as cli:
            managed.return_value.get_token.return_value = AccessToken("mi-token", int(time.time()) + 3600)
            self.assertEqual(model._bearer(), "mi-token")
            self.assertEqual(model._bearer(), "mi-token")
        managed.assert_called_once_with(client_id=identity)
        managed.return_value.get_token.assert_called_once_with(chat.MODEL_SCOPE)
        cli.assert_not_called()

    def test_missing_managed_identity_is_reported(self):
        model = chat.AzureOpenAIChat(ENDPOINT, "gpt-5-mini", TENANT, "low", "22222222-2222-4222-8222-222222222222")
        with patch("azure.identity.ManagedIdentityCredential") as managed:
            managed.return_value.get_token.side_effect = CredentialUnavailableError("No identity")
            with self.assertRaises(chat.ModelUnavailable) as caught:
                model._bearer()
        self.assertIn("managed identity", str(caught.exception))
        self.assertNotIn("az login", str(caught.exception))


class ChatRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()
        self.sid = "unit-test-chat"
        claims = {"sub": "alice-sub", "name": "Alice Rivera", "scp": "inventory.read", "exp": int(time.time()) + 3600}
        self.entry = {
            "user": USER, "id_token": "unused", "access_token": jwt.encode(claims, "k" * 32, algorithm="HS256"),
            "refresh_token": "unused", "expires_at": time.time() + 3600, "session_expires_at": time.time() + 3600,
            "renewals": 0, "chat": [], "chat_lock": threading.Lock(),
        }
        webapp.STORE[self.sid] = self.entry
        self.addCleanup(webapp.STORE.pop, self.sid, None)
        with self.client.session_transaction(base_url=config.WEB_URL) as session:
            session["sid"], session["csrf"] = self.sid, "csrf-value"
        self.mcp, self.sessions = FakeMcp(), []
        factory = patch.object(chat, "McpSession", side_effect=self.mcp_session)
        factory.start()
        self.addCleanup(factory.stop)
        self.use_model()

    def mcp_session(self, url, token_provider):
        self.sessions.append((url, token_provider(False)))
        return self.mcp

    def use_model(self, *replies):
        model = patch.object(webapp, "MODEL", FakeModel(*replies))
        model.start()
        self.addCleanup(model.stop)

    def post(self, path, body=None, csrf="csrf-value"):
        return self.client.post(path, json=body, headers={"X-CSRF-Token": csrf} if csrf else {},
                                base_url=config.WEB_URL)

    def test_chat_requires_a_session(self):
        response = webapp.app.test_client().post("/chat", json={"message": "hi"}, headers={"X-CSRF-Token": "x"},
                                                 base_url=config.WEB_URL)
        self.assertEqual((response.status_code, response.json["error"]), (401, "login_required"))

    def test_chat_and_reset_require_the_csrf_header(self):
        for path in ("/chat", "/chat/reset"):
            for csrf in (None, "wrong"):
                with self.subTest(path=path, csrf=csrf):
                    self.assertEqual(self.post(path, {"message": "hi"}, csrf=csrf).status_code, 403)
        self.assertEqual(self.sessions, [])

    def test_invalid_questions_are_rejected(self):
        for body in (None, {}, {"message": ""}, {"message": "   "}, {"message": 42}, ["hi"],
                     {"message": "x" * (webapp.MAX_QUESTION_CHARS + 1)}):
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.post("/chat", body).status_code, 400)
        self.assertEqual(self.sessions, [])

    def test_turn_runs_with_the_users_token_and_is_kept_in_history(self):
        self.use_model(wants_tools(tool_call({"sku": "WIDGET-001"})), says("42 at ATL-DEMO."))
        response = self.post("/chat", {"message": "  How many WIDGET-001?  "})
        self.assertEqual(response.status_code, 200)
        body = response.json
        self.assertEqual((body["answer"], body["engine"], body["error"], body["renewed"]),
                         ("42 at ATL-DEMO.", "fake-model", False, False))
        self.assertEqual(body["trace"][0]["result"]["quantity"], 42)
        self.assertEqual(body["token"]["sub"], "alice-sub")
        self.assertEqual(self.sessions, [(config.MCP_URL, (self.entry["access_token"], False))])
        self.assertEqual([(item["role"], item["content"]) for item in self.entry["chat"]],
                         [("user", "How many WIDGET-001?"), ("assistant", "42 at ATL-DEMO.")])
        self.assertFalse(self.entry["chat_lock"].locked())

    def test_history_is_trimmed(self):
        self.entry["chat"] = [{"role": "user", "content": f"q{n}"} for n in range(webapp.MAX_HISTORY_ITEMS)]
        self.use_model(says("latest"))
        self.assertEqual(self.post("/chat", {"message": "newest"}).status_code, 200)
        self.assertEqual(len(self.entry["chat"]), webapp.MAX_HISTORY_ITEMS)
        self.assertEqual([item["content"] for item in self.entry["chat"][-2:]], ["newest", "latest"])

    def test_overlapping_requests_are_rejected(self):
        self.entry["chat_lock"].acquire()
        self.addCleanup(self.entry["chat_lock"].release)
        self.assertEqual(self.post("/chat", {"message": "hi"}).status_code, 409)
        self.assertEqual(self.post("/chat/reset").status_code, 409)

    def test_revoked_refresh_token_ends_the_session(self):
        self.mcp = FakeMcp(list_error=webapp.NeedsLogin("invalid_grant"))
        response = self.post("/chat", {"message": "hi"})
        self.assertEqual((response.status_code, response.json["error"]), (401, "login_required"))
        self.assertNotIn(self.sid, webapp.STORE)
        self.assertFalse(self.entry["chat_lock"].locked())

    def test_upstream_failures_are_502_and_not_recorded(self):
        cases = (
            ("model_unavailable", FakeMcp(), [chat.ModelUnavailable("The model is rate limited.")]),
            ("mcp_unavailable", FakeMcp(list_error=chat.McpError(500, "server_error")), []),
            ("auth_unavailable", FakeMcp(list_error=webapp.AuthUnavailable("Token renewal failed.")), []),
            ("unavailable", FakeMcp(list_error=requests.ConnectionError("down")), []),
        )
        for error, mcp, replies in cases:
            with self.subTest(error=error):
                self.mcp = mcp
                self.use_model(*replies)
                response = self.post("/chat", {"message": "How many WIDGET-001?"})
                self.assertEqual((response.status_code, response.json["error"]), (502, error))
                self.assertEqual(self.entry["chat"], [])
                self.assertFalse(self.entry["chat_lock"].locked())

    def test_reset_clears_history(self):
        self.entry["chat"] = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        self.assertEqual(self.post("/chat/reset").status_code, 200)
        self.assertEqual(self.entry["chat"], [])

    def test_page_renders_history_and_questions_as_inert_data(self):
        self.entry["chat"] = [{"role": "user", "content": "</script><script>alert(1)</script>"}]
        with patch.object(webapp, "TEST_QUESTIONS", [{"label": "1. Check <WIDGET-001>", "text": "Check it"}]):
            response = self.client.get("/", base_url=config.WEB_URL)
        page = response.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("\\u003c/script\\u003e\\u003cscript\\u003ealert(1)", page)
        self.assertIn('<option value="0">1. Check &lt;WIDGET-001&gt;</option>', page)
        self.assertIn('content="csrf-value"', page)
        nonce = re.search(r"script-src 'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
        self.assertIn(f'<script nonce="{nonce}">', page)

    def test_signed_out_page_offers_sign_in_only(self):
        page = webapp.app.test_client().get("/", base_url=config.WEB_URL).get_data(as_text=True)
        self.assertIn('href="/login"', page)
        self.assertNotIn("csrf-token", page)
        self.assertNotIn('id="composer"', page)


class TestQuestionTests(unittest.TestCase):
    def test_picker_reads_string_queries_only(self):
        lines = [json.dumps({"id": 1, "query": "Check WIDGET-001.\nReturn exactly two lines."}), "not json",
                 json.dumps({"id": 2, "query": ["structured"]}), json.dumps({"id": 3, "query": "   "}),
                 json.dumps({"id": 4, "query": "x" * 200}), json.dumps(["not", "an", "object"])]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "questions.jsonl")
            path.write_text("\n".join(lines), encoding="utf-8")
            questions = webapp.load_test_questions(str(path))
        self.assertEqual(questions[0], {"label": "1. Check WIDGET-001.",
                                        "text": "Check WIDGET-001.\nReturn exactly two lines."})
        self.assertEqual([len(question["label"]) for question in questions], [20, 90])
        self.assertEqual(webapp.load_test_questions(str(path)), [])
        self.assertEqual(webapp.load_test_questions(None), [])

    @unittest.skipUnless(Path(config.TEST_QUESTIONS_FILE).is_file(), "evaluation dataset not present")
    def test_evaluation_dataset_populates_the_picker(self):
        questions = webapp.load_test_questions(config.TEST_QUESTIONS_FILE)
        self.assertGreater(len(questions), 0)
        self.assertTrue(all(question["text"] and question["label"] for question in questions))


class ModelConfigurationTests(unittest.TestCase):
    def load(self, show="AZURE_OPENAI_ENDPOINT", **settings):
        env = {**os.environ, "AUTH_MODE": "mock", "AZURE_OPENAI_ENDPOINT": "", **settings}
        return subprocess.run([sys.executable, "-c", f"import config; print(config.{show})"],
                              cwd=Path(__file__).parent, env=env, capture_output=True, text=True, timeout=20)

    def test_resource_endpoint_is_accepted_and_normalized(self):
        result = self.load(AZURE_OPENAI_ENDPOINT="https://demo.openai.azure.com/")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "https://demo.openai.azure.com"), result.stderr)

    def test_unsafe_model_settings_are_rejected(self):
        for settings in (
            {"AZURE_OPENAI_ENDPOINT": "http://demo.openai.azure.com"},
            {"AZURE_OPENAI_ENDPOINT": "https://demo.openai.azure.com.evil.example"},
            {"AZURE_OPENAI_ENDPOINT": "https://demo.openai.azure.com/openai/v1"},
            {"AZURE_OPENAI_ENDPOINT": "https://demo.openai.azure.com:8443"},
            {"AZURE_OPENAI_ENDPOINT": "https://user@demo.openai.azure.com"},
            {"AZURE_OPENAI_DEPLOYMENT": "../other"},
            {"MODEL_REASONING_EFFORT": "extreme"},
            {"AZURE_OPENAI_TENANT_ID": "not-a-tenant"},
        ):
            with self.subTest(settings=settings):
                self.assertNotEqual(self.load(**settings).returncode, 0)

    def test_deployment_settings_are_validated_and_defaulted(self):
        identity = "22222222-2222-4222-8222-222222222222"
        for show, settings, expected in (
            ("MODEL_IDENTITY_CLIENT_ID", {"MODEL_IDENTITY_CLIENT_ID": ""}, "None"),
            ("MODEL_IDENTITY_CLIENT_ID", {"MODEL_IDENTITY_CLIENT_ID": identity.upper()}, identity),
            ("WEB_BIND_HOST", {"WEB_BIND_HOST": ""}, "127.0.0.1"),
            ("WEB_BIND_HOST", {"WEB_BIND_HOST": "0.0.0.0"}, "0.0.0.0"),
        ):
            with self.subTest(settings=settings):
                result = self.load(show, **settings)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)
        for settings in ({"MODEL_IDENTITY_CLIENT_ID": "not-a-client-id"}, {"WEB_BIND_HOST": "example.com"},
                         {"WEB_BIND_HOST": "0.0.0.0/0"}):
            with self.subTest(settings=settings):
                self.assertNotEqual(self.load(**settings).returncode, 0)

if __name__ == "__main__":
    unittest.main()
