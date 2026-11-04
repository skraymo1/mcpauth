"""Tiny OIDC provider that stands in for Microsoft Entra ID so the demo runs offline.

It implements just enough of authorization-code + PKCE, refresh-token rotation, JWKS and
discovery to behave like a real IdP. It keeps an SSO session cookie, so after the first
password prompt every later sign-in is silent. Do not use for anything real.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, jsonify, redirect, render_template_string, request, session

import config

log = logging.getLogger("idp")

USERS = {
    "alice": {"password": "demo", "name": "Alice Rivera", "roles": ["Inventory.Reader"]},
    "bob": {"password": "demo", "name": "Bob Chen", "roles": []},
}
CLIENTS = {config.CLIENT_ID: {"secret": config.CLIENT_SECRET, "redirect_uris": [config.REDIRECT_URI]}}
API_SCOPES = {config.API_SCOPE}
REFRESH_TTL = 8 * 3600

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = uuid.uuid4().hex[:8]
CODES: dict[str, dict] = {}
REFRESH_TOKENS: dict[str, dict] = {}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME="idp_session",  # cookies are shared across ports on one host
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

LOGIN_PAGE = """<!doctype html><title>Mock Entra sign-in</title>
<body style="font:16px system-ui;max-width:380px;margin:80px auto">
<h2>Sign in</h2><p style="color:#555">Mock identity provider (stand-in for Microsoft Entra ID)</p>
{% if error %}<p style="color:#b00">{{ error }}</p>{% endif %}
<form method="post" action="/login">
<input type="hidden" name="query" value="{{ query }}">
<p><input name="username" placeholder="alice or bob" autofocus style="width:100%;padding:8px"></p>
<p><input name="password" type="password" placeholder="demo" style="width:100%;padding:8px"></p>
<p><button style="padding:8px 16px">Sign in</button></p></form>
<p style="color:#555;font-size:14px">alice / demo has the Inventory.Reader role.<br>
bob / demo has no role.</p></body>"""


def _sign(claims: dict) -> str:
    return jwt.encode(claims, _key, algorithm="RS256", headers={"kid": KID})


def _redirect_back(redirect_uri: str, **params):
    params = {k: v for k, v in params.items() if v is not None}
    return redirect(f"{redirect_uri}?{urlencode(params)}")


@app.get("/.well-known/openid-configuration")
def discovery():
    base = config.ISSUER
    return jsonify(
        issuer=base,
        authorization_endpoint=f"{base}/authorize",
        token_endpoint=f"{base}/token",
        jwks_uri=f"{base}/jwks.json",
        end_session_endpoint=f"{base}/logout",
        response_types_supported=["code"],
        subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"],
        code_challenge_methods_supported=["S256"],
        token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"],
        grant_types_supported=["authorization_code", "refresh_token"],
        scopes_supported=["openid", "profile", "offline_access", *API_SCOPES],
    )


@app.get("/jwks.json")
def jwks():
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_key.public_key(), as_dict=True)
    return jsonify(keys=[{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}])


@app.get("/authorize")
def authorize():
    q = request.args
    client = CLIENTS.get(q.get("client_id", ""))
    if not client or q.get("redirect_uri") not in client["redirect_uris"]:
        return "Unknown client or redirect_uri", 400
    redirect_uri, state = q["redirect_uri"], q.get("state")
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" \
            or not q.get("code_challenge"):
        return _redirect_back(redirect_uri, error="invalid_request", state=state,
                              error_description="authorization code flow with PKCE S256 required")
    username = session.get("user")
    if not username:
        if q.get("prompt") == "none":
            return _redirect_back(redirect_uri, error="login_required", state=state)
        return render_template_string(LOGIN_PAGE, query=request.query_string.decode(), error=None)
    code = secrets.token_urlsafe(24)
    CODES[code] = {
        "user": username, "client_id": q["client_id"], "redirect_uri": redirect_uri,
        "challenge": q["code_challenge"], "scope": q.get("scope", ""), "nonce": q.get("nonce"),
        "exp": time.time() + 60,
    }
    log.info("silent SSO: %s already signed in, issuing code to %s", username, q["client_id"])
    return _redirect_back(redirect_uri, code=code, state=state)


@app.post("/login")
def login():
    username = request.form.get("username", "").strip().lower()
    user = USERS.get(username)
    supplied = request.form.get("password", "")
    if not user or not hmac.compare_digest(supplied, user["password"]):
        return render_template_string(LOGIN_PAGE, query=request.form.get("query", ""),
                                      error="Wrong username or password."), 401
    session.clear()
    session["user"] = username
    log.info("interactive login: %s (the only time a password is typed)", username)
    return redirect("/authorize?" + request.form.get("query", ""))


@app.get("/logout")
def logout():
    session.clear()
    target = request.args.get("post_logout_redirect_uri")
    if target == f"{config.WEB_URL}/":
        return redirect(target)
    return "Signed out of the identity provider."


def _error(error: str, status: int = 400):
    return jsonify(error=error), status


def _authenticate_client():
    if request.authorization:
        return request.authorization.username, request.authorization.password
    return request.form.get("client_id"), request.form.get("client_secret")


def _issue_tokens(username: str, client_id: str, scope: str, nonce: str | None, with_id_token: bool):
    user, now = USERS[username], int(time.time())
    requested = set(scope.split())
    api_scopes = sorted(requested & API_SCOPES)
    access = _sign({
        "iss": config.ISSUER, "aud": config.API_AUDIENCE, "sub": username, "azp": client_id,
        "name": user["name"], "preferred_username": f"{username}@contoso.example",
        "scp": " ".join(api_scopes), "roles": user["roles"], "ver": "2.0",
        "iat": now, "nbf": now, "exp": now + config.ACCESS_TOKEN_TTL, "jti": uuid.uuid4().hex,
    })
    body = {"token_type": "Bearer", "access_token": access, "expires_in": config.ACCESS_TOKEN_TTL,
            "scope": " ".join(api_scopes)}
    if with_id_token:
        body["id_token"] = _sign({
            "iss": config.ISSUER, "aud": client_id, "sub": username, "name": user["name"],
            "preferred_username": f"{username}@contoso.example", "nonce": nonce,
            "iat": now, "exp": now + 3600, "auth_time": now,
        })
    if "offline_access" in requested:
        refresh = secrets.token_urlsafe(32)
        REFRESH_TOKENS[refresh] = {"user": username, "client_id": client_id, "scope": scope,
                                   "exp": time.time() + REFRESH_TTL}
        body["refresh_token"] = refresh
    return body


@app.post("/token")
def token():
    client_id, secret = _authenticate_client()
    client = CLIENTS.get(client_id or "")
    if not client or not hmac.compare_digest(secret or "", client["secret"]):
        return _error("invalid_client", 401)
    grant = request.form.get("grant_type")
    if grant == "authorization_code":
        code = CODES.pop(request.form.get("code", ""), None)
        verifier = request.form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if (not code or code["exp"] < time.time() or code["client_id"] != client_id
                or code["redirect_uri"] != request.form.get("redirect_uri")
                or not hmac.compare_digest(challenge, code["challenge"])):
            return _error("invalid_grant")
        log.info("token issued (authorization_code) for %s, access token lifetime %ss",
                 code["user"], config.ACCESS_TOKEN_TTL)
        return jsonify(_issue_tokens(code["user"], client_id, code["scope"], code["nonce"], True))
    if grant == "refresh_token":
        old = REFRESH_TOKENS.pop(request.form.get("refresh_token", ""), None)
        if not old or old["exp"] < time.time() or old["client_id"] != client_id:
            return _error("invalid_grant")
        log.info("token issued (refresh_token, rotated) for %s - no user interaction", old["user"])
        return jsonify(_issue_tokens(old["user"], client_id, old["scope"], None, False))
    return _error("unsupported_grant_type")


def main():
    app.run(host=config.HOST, port=config.IDP_PORT)


if __name__ == "__main__":
    main()
