"""Minimal MCP server (Streamable HTTP, JSON responses) protected by per-user OAuth tokens.

It is an OAuth *resource server*: it accepts only access tokens issued for its own audience,
verifies them against the IdP's published signing keys, and authorizes the individual user.
It never forwards the incoming token to another service.
"""
import json
import logging
import threading

import jwt
import requests
from flask import Flask, jsonify, request

import config

log = logging.getLogger("mcp")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26"]
RESOURCE = config.MCP_URL
METADATA_URL = f"{config.MCP_PUBLIC_URL}/.well-known/oauth-protected-resource"
TOOL = {
    "name": "get_inventory",
    "description": "Read fictional inventory for a demo SKU. No production data.",
    "inputSchema": {
        "type": "object",
        "properties": {"sku": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["sku"],
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

_jwk_client = None
_jwk_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, status, error=None, description=None, scope=None):
        self.status, self.error, self.description, self.scope = status, error, description, scope


def _signing_keys() -> jwt.PyJWKClient:
    global _jwk_client
    with _jwk_lock:
        if _jwk_client is None:
            try:
                response = requests.get(config.DISCOVERY_URL, timeout=5, allow_redirects=False)
                response.raise_for_status()
                meta = response.json()
                if meta["issuer"] != config.ISSUER:
                    raise ValueError("Discovery issuer mismatch")
                if not config.USE_MOCK and not meta["jwks_uri"].startswith("https://"):
                    raise ValueError("Entra signing keys must use HTTPS")
                _jwk_client = jwt.PyJWKClient(meta["jwks_uri"], timeout=5)
            except (requests.RequestException, KeyError, ValueError):
                log.error("Identity provider metadata unavailable or invalid")
                raise AuthError(503, description="Identity provider metadata unavailable")
    return _jwk_client


def authenticate() -> dict:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError(401, description="Bearer access token required")
    try:
        key = _signing_keys().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=config.API_AUDIENCE, issuer=config.ISSUER,
            options={"require": ["exp", "iss", "aud", "sub"]}, leeway=5,
        )
    except jwt.PyJWKClientConnectionError:
        raise AuthError(503, description="Signing keys unavailable")
    except jwt.PyJWTError as exc:
        log.warning(json.dumps({"event": "token_rejected", "reason": type(exc).__name__}))
        raise AuthError(401, "invalid_token", type(exc).__name__)
    if not config.USE_MOCK and (
        claims.get("tid") != config.TENANT_ID or claims.get("ver") != "2.0"
        or claims.get("azp") != config.CLIENT_ID
        or not isinstance(claims.get("oid"), str) or not claims["oid"]
    ):
        log.warning("Entra tenant, version, client, or user identity rejected")
        raise AuthError(401, "invalid_token", "Token does not match the configured Entra application")
    scopes = claims.get("scp")
    if not isinstance(scopes, str) or config.REQUIRED_SCOPE not in scopes.split():
        raise AuthError(403, "insufficient_scope", "Delegated scope missing", config.REQUIRED_SCOPE)
    roles = claims.get("roles")
    if not isinstance(roles, list) or config.REQUIRED_ROLE not in roles:
        log.warning(json.dumps({"event": "user_not_authorized", "user": claims.get("preferred_username")}))
        raise AuthError(403, "access_denied", f"User lacks the {config.REQUIRED_ROLE} role")
    return claims


@app.errorhandler(AuthError)
def auth_error(err: AuthError):
    params = ['realm="inventory-mcp"', f'resource_metadata="{METADATA_URL}"']
    if err.error:
        params.append(f'error="{err.error}"')
    if err.scope:
        params.append(f'scope="{err.scope}"')
    resp = jsonify(error=err.error or "unauthorized", error_description=err.description)
    resp.status_code = err.status
    if err.status in (401, 403):
        resp.headers["WWW-Authenticate"] = "Bearer " + ", ".join(params)
    return resp


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata():
    # RFC 9728: tells an MCP client which authorization server to sign the user in with.
    return jsonify(
        resource=RESOURCE,
        authorization_servers=[config.ISSUER],
        scopes_supported=[config.API_SCOPE],
        bearer_methods_supported=["header"],
    )


def _rpc(msg_id, *, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": msg_id}
    body.update({"error": error} if error else {"result": result})
    return jsonify(body)


def _call_tool(params: dict, claims: dict):
    args = params.get("arguments") or {}
    sku = args.get("sku")
    if params.get("name") != TOOL["name"] or not isinstance(sku, str):
        return {"isError": True, "content": [{"type": "text", "text": "Unknown tool or bad arguments."}]}
    user = claims.get("preferred_username", claims["sub"])
    log.info(json.dumps({"event": "tool_call", "user": user, "sub": claims["sub"],
                         "tenantId": claims.get("tid"), "objectId": claims.get("oid"),
                         "tool": TOOL["name"], "sku": sku}))
    if sku != "WIDGET-001":
        return {"isError": True, "content": [{"type": "text", "text": "Unknown demo SKU; use WIDGET-001."}]}
    result = {"sku": sku, "quantity": 42, "warehouse": "ATL-DEMO",
              "source": "fictional-demo-data", "requested_by": user}
    if not config.USE_MOCK:
        result["principal"] = {"tenant_id": claims["tid"], "object_id": claims["oid"]}
    return {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}


@app.post("/mcp")
def mcp():
    origin = request.headers.get("Origin")
    if origin and origin != config.MCP_PUBLIC_URL:
        return jsonify(error="Origin not allowed"), 403
    claims = authenticate()
    msg = request.get_json(silent=True)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc(None, error={"code": -32600, "message": "Invalid Request"}), 400
    msg_id, method = msg.get("id"), msg.get("method")
    if msg_id is None:
        return "", 202
    params = msg.get("params") or {}
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _rpc(msg_id, result={
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "aws-inventory-demo", "version": "1.0.0"},
        })
    if method == "ping":
        return _rpc(msg_id, result={})
    if method == "tools/list":
        return _rpc(msg_id, result={"tools": [TOOL]})
    if method == "tools/call":
        return _rpc(msg_id, result=_call_tool(params, claims))
    return _rpc(msg_id, error={"code": -32601, "message": "Method not found"})


@app.get("/mcp")
def mcp_get():
    return jsonify(error="This stateless MCP endpoint supports POST only."), 405, {"Allow": "POST"}


def main():
    app.run(host=config.HOST, port=config.MCP_PORT)


if __name__ == "__main__":
    main()
