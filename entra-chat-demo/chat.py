"""Chat orchestration: a model chooses tool calls; the app runs them over MCP as the signed-in user.

The model never receives OAuth tokens. The user's delegated access token is sent only to the
MCP server, and only tools that the server marks read-only run without an approval step.
"""
import itertools
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("chat")

PROTOCOL_VERSION = "2025-06-18"
MAX_TOOL_ROUNDS = 4
MAX_CALLS_PER_ROUND = 5
MAX_TOOL_OUTPUT = 8000
MODEL_SCOPE = "https://cognitiveservices.azure.com/.default"
OFFLINE_ENGINE = "Offline responder (no model configured)"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
SKU_WORD = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\b")

SYSTEM_PROMPT = (
    "You are the inventory assistant in a sign-in-once demo. Use the available tools for inventory "
    "questions, with one lookup per SKU the user asks about. Never invent quantities, warehouses, SKUs, "
    "or identifiers; if a lookup fails or a SKU is unknown, say so plainly. Tools run as the signed-in "
    "user with that user's own delegated access token, so an access error means this user lacks "
    "permission. If the user asks for an exact output format, reply in exactly that format with no "
    "extra text; otherwise answer in one to three short sentences.\n"
    "Signed-in user: {name} ({username})."
)


class ModelUnavailable(Exception):
    pass


class McpError(Exception):
    def __init__(self, status: int, error: str | None = None, description: str | None = None):
        super().__init__(description or error or f"HTTP {status}")
        self.status, self.error, self.description = status, error, description


@dataclass
class Turn:
    answer: str
    engine: str
    trace: list = field(default_factory=list)
    error: bool = False


class McpSession:
    """Streamable HTTP MCP client that always calls as the signed-in user."""

    def __init__(self, url: str, token_provider):
        self.url = url
        self.token_provider = token_provider  # (force_refresh) -> (access_token, renewed)
        self.protocol = None
        self.renewed = False
        self._ids = itertools.count(1)

    def list_tools(self) -> list:
        tools = self._request("tools/list").get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _request(self, method: str, params: dict | None = None) -> dict:
        if self.protocol is None:
            self._initialize()
        return self._result(self._post(
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}))

    def _initialize(self):
        result = self._result(self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "entra-chat-demo", "version": "1.0.0"}}}))
        if not isinstance(result.get("protocolVersion"), str):
            raise McpError(502, "invalid_response", "MCP initialization did not return a protocol version.")
        self.protocol = result["protocolVersion"]
        response = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if response.status_code != 202:
            raise McpError(response.status_code, "initialize_failed", "MCP initialization notification failed.")

    def _post(self, payload: dict) -> requests.Response:
        token, renewed = self.token_provider(False)
        self.renewed |= renewed
        response = self._send(token, payload)
        if response.status_code == 401:
            # The token can expire or be revoked between the local expiry check and the call.
            token, renewed = self.token_provider(True)
            self.renewed |= renewed
            response = self._send(token, payload)
        return response

    def _send(self, token: str, payload: dict) -> requests.Response:
        headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return requests.post(self.url, json=payload, headers=headers, timeout=15, allow_redirects=False)

    @staticmethod
    def _result(response: requests.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            body = body if isinstance(body, dict) else {}
            error, description = body.get("error"), body.get("error_description")
            raise McpError(response.status_code, error if isinstance(error, str) else None,
                           description if isinstance(description, str) else "Unreadable MCP response.")
        if "error" in body:
            error = body["error"] if isinstance(body["error"], dict) else {}
            raise McpError(200, "jsonrpc_error", str(error.get("message") or "MCP request failed."))
        if not isinstance(body.get("result"), dict):
            raise McpError(502, "invalid_response", "MCP returned no result.")
        return body["result"]


class AzureOpenAIChat:
    """Chat Completions on an Azure OpenAI deployment, called with this app's Azure identity.

    Locally that identity is the developer's `az login`; when deployed it is a user-assigned
    managed identity. The signed-in user's token is never sent to the model endpoint.
    """

    def __init__(self, endpoint: str, deployment: str, tenant_id: str | None, reasoning_effort: str,
                 identity_client_id: str | None = None):
        self.url = f"{endpoint}/openai/v1/chat/completions"
        self.deployment, self.tenant_id, self.reasoning_effort = deployment, tenant_id, reasoning_effort
        self.identity_client_id = identity_client_id
        self.label = f"{deployment} (Azure OpenAI)"
        self._credential = None
        self._token = None
        self._lock = threading.Lock()

    def _bearer(self, renew: bool = False) -> str:
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import AzureCliCredential, ManagedIdentityCredential

        with self._lock:
            if renew or self._token is None or self._token.expires_on - time.time() < 300:
                if self._credential is None:
                    self._credential = (
                        ManagedIdentityCredential(client_id=self.identity_client_id) if self.identity_client_id
                        else AzureCliCredential(tenant_id=self.tenant_id, process_timeout=30))
                try:
                    self._token = self._credential.get_token(MODEL_SCOPE)
                except ClientAuthenticationError:
                    log.error("Azure credential unavailable for the model call")
                    raise ModelUnavailable(
                        "The app's managed identity is unavailable; check its assignment on the app."
                        if self.identity_client_id else
                        "The app's Azure credential is unavailable. Run az login for the model's tenant.") from None
            return self._token.token

    def complete(self, messages: list, tools: list, tool_choice: str | None = None) -> dict:
        body = {"model": self.deployment, "messages": messages,
                "reasoning_effort": self.reasoning_effort, "max_completion_tokens": 4000}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        try:
            for attempt in range(2):
                response = requests.post(self.url, json=body, timeout=90, allow_redirects=False,
                                         headers={"Authorization": f"Bearer {self._bearer(renew=attempt > 0)}"})
                if response.status_code != 401:
                    break
        except requests.RequestException as exc:
            log.error("Model endpoint unreachable: %s", type(exc).__name__)
            raise ModelUnavailable("The model endpoint is unreachable; try again.") from None
        if response.status_code != 200:
            log.error("Model call failed: HTTP %s %s", response.status_code, _error_code(response))
            raise ModelUnavailable(_model_error(response.status_code))
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            message = None
        if not isinstance(message, dict):
            raise ModelUnavailable("The model returned an unreadable response.")
        return message


def _error_code(response: requests.Response) -> str:
    try:
        error = response.json().get("error")
        return str(error.get("code", "")) if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        return ""


def _model_error(status: int) -> str:
    if status in (401, 403):
        return (f"The model rejected the app's Azure credential (HTTP {status}). Check az login (or the "
                "managed identity when deployed) and the Foundry User or Cognitive Services OpenAI User "
                "role on the model resource.")
    if status == 404:
        return "The model deployment was not found (HTTP 404). Check AZURE_OPENAI_DEPLOYMENT."
    if status == 429:
        return "The model is rate limited (HTTP 429); try again shortly."
    return f"The model call failed (HTTP {status})."


def function_tools(tools: list) -> list:
    """Offer the model only the read-only MCP tools, converted to function-tool definitions."""
    offered = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        if annotations.get("readOnlyHint") is not True or not TOOL_NAME.fullmatch(tool["name"]):
            continue
        schema = tool.get("inputSchema")
        offered.append({"type": "function", "function": {
            "name": tool["name"], "description": str(tool.get("description", ""))[:1024],
            "parameters": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        }})
    return offered


def run_tool(call: dict, allowed: set, mcp: McpSession, trace: list) -> str:
    """Run one model-requested tool call as the user; return the text given back to the model."""
    function = call.get("function") if isinstance(call, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    step = {"tool": name if isinstance(name, str) else "unknown", "ok": False}
    trace.append(step)
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (AttributeError, TypeError, ValueError):
        arguments = None
    if not isinstance(arguments, dict):
        step["detail"] = "The model sent invalid tool arguments."
        return json.dumps({"error": step["detail"]})
    step["arguments"] = arguments
    if name not in allowed:
        step["detail"] = "Tool not available; only read-only tools run without approval."
        return json.dumps({"error": step["detail"]})
    try:
        result = mcp.call_tool(name, arguments)
    except McpError as exc:
        step.update(status=exc.status, detail=exc.description or exc.error or f"HTTP {exc.status}")
        return json.dumps({"error": step["detail"], "http_status": exc.status})
    step["status"] = 200
    text = "\n".join(item["text"] for item in result.get("content") or []
                     if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
    if result.get("isError"):
        step["detail"] = text or "The tool reported an error."
        return json.dumps({"error": step["detail"]})
    step["ok"] = True
    structured = result.get("structuredContent")
    step["result"] = structured if isinstance(structured, dict) else text[:MAX_TOOL_OUTPUT]
    return (json.dumps(structured) if isinstance(structured, dict) else text)[:MAX_TOOL_OUTPUT]


def find_skus(text: str) -> list[str]:
    skus = []
    for word in SKU_WORD.findall(text):
        if (word.isupper() or any(ch.isdigit() for ch in word)) and word.upper() not in skus:
            skus.append(word.upper())
    return skus[:3]


def offline_answer(question: str, allowed: set, mcp: McpSession, trace: list) -> str:
    """Deterministic stand-in for a model: look up each SKU mentioned in the question."""
    skus = find_skus(question)
    if not skus:
        return "Ask me about a SKU, for example: How many WIDGET-001 do we have?"
    lines = []
    for sku in skus:
        run_tool({"function": {"name": "get_inventory", "arguments": json.dumps({"sku": sku})}}, allowed, mcp, trace)
        step = trace[-1]
        if step["ok"] and isinstance(step.get("result"), dict):
            lines.append(f"{sku}: {step['result'].get('quantity')} in stock at {step['result'].get('warehouse')}.")
        else:
            lines.append(f"{sku}: {step.get('detail')}")
    return "\n".join(lines)


def final_text(reply: dict) -> str:
    content = reply.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return "The model returned no answer. Try rephrasing the question."


def run_turn(question: str, history: list, *, user: dict, mcp: McpSession, model) -> Turn:
    engine = model.label if model else OFFLINE_ENGINE
    try:
        tools = function_tools(mcp.list_tools())
    except McpError as exc:
        if exc.status not in (401, 403):
            raise
        detail = exc.description or exc.error or "access denied"
        return Turn(f"The inventory service refused your request (HTTP {exc.status}): {detail}.", engine,
                    [{"tool": "tools/list", "ok": False, "status": exc.status, "detail": detail}], error=True)
    allowed = {tool["function"]["name"] for tool in tools}
    trace = []
    if model is None:
        return Turn(offline_answer(question, allowed, mcp, trace), engine, trace)
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(
        name=user.get("name", "unknown"), username=user.get("preferred_username", "unknown"))}]
    messages += [{"role": item["role"], "content": item["content"]} for item in history]
    messages.append({"role": "user", "content": question})
    for _ in range(MAX_TOOL_ROUNDS):
        reply = model.complete(messages, tools)
        calls = reply.get("tool_calls") or []
        if not calls:
            return Turn(final_text(reply), engine, trace)
        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
        for index, call in enumerate(calls):
            output = (run_tool(call, allowed, mcp, trace) if index < MAX_CALLS_PER_ROUND
                      else json.dumps({"error": "Too many tool calls in one step."}))
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": output})
    return Turn(final_text(model.complete(messages, tools, tool_choice="none")), engine, trace)
