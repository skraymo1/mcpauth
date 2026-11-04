"""Chat web app demonstrating sign-in once with per-user OAuth tokens to an MCP server.

The user signs in (OIDC authorization code + PKCE) and chats. A model decides when to look up
inventory; the app calls the MCP server as the signed-in user with their delegated access token
and renews it silently with the refresh token - no API key, no second login.
"""
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import jwt
import requests
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.flask_client import OAuth
from flask import Flask, g, jsonify, redirect, render_template_string, request, session, url_for

import chat
import config

log = logging.getLogger("web")
app = Flask(__name__)
app.config.update(
    SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME="web_session",  # cookies are shared across ports on one host
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.WEB_URL.startswith("https://"),
    TRUSTED_HOSTS=[urlsplit(config.WEB_URL).hostname],
    MAX_CONTENT_LENGTH=32 * 1024,
)

oauth = OAuth(app)
oauth.register(
    name="idp",
    client_id=config.CLIENT_ID,
    client_secret=config.CLIENT_SECRET,
    server_metadata_url=config.DISCOVERY_URL,
    client_kwargs={
        "scope": config.LOGIN_SCOPES, "code_challenge_method": "S256",
        "token_endpoint_auth_method": "client_secret_post",
    },
)

# Tokens live on the server; the browser only holds an opaque session id.
STORE: dict[str, dict] = {}
_refresh_lock = threading.Lock()
SKEW_SECONDS = 20
MAX_QUESTION_CHARS = 4000
MAX_HISTORY_ITEMS = 20

MODEL = (chat.AzureOpenAIChat(config.AZURE_OPENAI_ENDPOINT, config.AZURE_OPENAI_DEPLOYMENT,
                              config.MODEL_TENANT_ID, config.MODEL_REASONING_EFFORT,
                              config.MODEL_IDENTITY_CLIENT_ID)
         if config.AZURE_OPENAI_ENDPOINT else None)
ENGINE = MODEL.label if MODEL else chat.OFFLINE_ENGINE
SUGGESTIONS = [
    "How many WIDGET-001 do we have, and in which warehouse?",
    "Is TEST-404 in stock?",
    "Use the AWS inventory tool to check WIDGET-001. Tell me the quantity and warehouse. "
    "Do not invent a result if the tool fails.",
]


def load_test_questions(path: str | None) -> list[dict]:
    """Picker entries from an evaluation JSONL file with string "query" fields."""
    if not path or not Path(path).is_file():
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        log.warning("Test question file is unreadable; the picker is hidden")
        return []
    questions = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        query = item.get("query") if isinstance(item, dict) else None
        if isinstance(query, str) and query.strip() and len(query) <= MAX_QUESTION_CHARS:
            label = f"{item.get('id', len(questions) + 1)}. {query.strip().splitlines()[0]}"
            questions.append({"label": label[:90], "text": query.strip()})
        if len(questions) == 50:
            break
    return questions


TEST_QUESTIONS = load_test_questions(config.TEST_QUESTIONS_FILE)


class NeedsLogin(Exception):
    pass


class AuthUnavailable(Exception):
    pass


def current_entry():
    entry = STORE.get(session.get("sid", ""))
    if entry and entry["session_expires_at"] <= time.time():
        STORE.pop(session.pop("sid", ""), None)
        return None
    return entry


def token_lifetime(body: dict) -> float:
    lifetime = body.get("expires_in")
    if (not isinstance(body.get("access_token"), str) or not body["access_token"]
            or str(body.get("token_type", "")).lower() != "bearer"
            or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or lifetime <= 0):
        raise AuthUnavailable("Identity provider returned an invalid token response.")
    return time.time() + lifetime


def access_token_for(entry: dict, force_refresh: bool = False) -> tuple[str, bool]:
    """Return a valid access token, renewing it silently when it is about to expire."""
    with _refresh_lock:
        if not force_refresh and entry["expires_at"] - time.time() > SKEW_SECONDS:
            return entry["access_token"], False
        if not entry.get("refresh_token"):
            raise NeedsLogin("no refresh token")
        endpoint = oauth.idp.load_server_metadata()["token_endpoint"]
        resp = requests.post(endpoint, timeout=10, allow_redirects=False, data={
            "grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
            "client_id": config.CLIENT_ID, "client_secret": config.CLIENT_SECRET,
            "scope": config.LOGIN_SCOPES,
        })
        try:
            body = resp.json()
        except ValueError:
            raise AuthUnavailable("Identity provider returned an unreadable response.") from None
        if not isinstance(body, dict):
            raise AuthUnavailable("Identity provider returned an invalid response.")
        if resp.status_code != 200:
            if body.get("error") in ("invalid_grant", "interaction_required", "login_required", "consent_required"):
                raise NeedsLogin("Microsoft requires sign-in or consent again")
            raise AuthUnavailable(f"Token renewal failed (HTTP {resp.status_code}); the session was retained.")
        expires_at = token_lifetime(body)
        entry.update(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", entry["refresh_token"]),
            expires_at=expires_at,
        )
        entry["renewals"] += 1
        log.info("Access token renewed without browser interaction")
        return entry["access_token"], True


def display_claims(token: str) -> dict:
    # Display only. Real clients treat access tokens as opaque; the MCP server does the validating.
    claims = jwt.decode(token, options={"verify_signature": False})
    return {k: claims[k] for k in ("iss", "tid", "oid", "sub", "name", "aud", "azp", "scp", "roles", "exp")
            if k in claims}


def csrf_ok() -> bool:
    expected = session.get("csrf")
    return bool(expected) and secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), expected)


@app.before_request
def csp_nonce():
    g.nonce = secrets.token_urlsafe(16)


@app.after_request
def response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    nonce = g.get("nonce", "")
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return response


@app.get("/healthz")
def health():
    return jsonify(status="ok", authentication=config.AUTH_MODE, engine=ENGINE)


def login_error(message: str, status: int):
    return render_template_string(
        '<!doctype html><title>Sign-in unavailable</title><h1>Sign-in unavailable</h1>'
        '<p>{{ message }}</p><a href="/login">Try signing in again</a>',
        message=message,
    ), status


@app.get("/")
def index():
    entry = current_entry()
    if not entry:
        return render_template_string(PAGE, user=None, use_mock=config.USE_MOCK, engine=ENGINE)
    return render_template_string(
        PAGE, user=entry["user"], claims=display_claims(entry["access_token"]), csrf=session.get("csrf"),
        history=entry["chat"], questions=TEST_QUESTIONS, suggestions=SUGGESTIONS,
        use_mock=config.USE_MOCK, engine=ENGINE, max_chars=MAX_QUESTION_CHARS)


@app.get("/login")
def login():
    try:
        return oauth.idp.authorize_redirect(config.REDIRECT_URI, response_mode="query")
    except (requests.RequestException, AuthlibBaseError) as exc:
        log.error("Unable to start sign-in: %s", type(exc).__name__)
        return login_error("The identity provider is unavailable. Please try again.", 502)


@app.get("/auth/callback")
def callback():
    try:
        token = oauth.idp.authorize_access_token(timeout=10, allow_redirects=False)
        expires_at = token_lifetime(token)
        if not isinstance(token.get("userinfo"), dict) or not token.get("id_token"):
            raise AuthUnavailable("A verified identity token was not returned.")
    except AuthlibBaseError as exc:
        log.warning("Sign-in rejected: %s", type(exc).__name__)
        return login_error("Sign-in was canceled or rejected. Check your tenant, app assignment, and consent.", 400)
    except (requests.RequestException, AuthUnavailable) as exc:
        log.error("Sign-in unavailable: %s", type(exc).__name__)
        return login_error("Token acquisition failed. Check the app configuration or try again.", 502)
    sid = secrets.token_urlsafe(24)
    STORE.pop(session.get("sid", ""), None)
    STORE[sid] = {
        "user": token["userinfo"], "id_token": token["id_token"],
        "access_token": token["access_token"], "refresh_token": token.get("refresh_token"),
        "expires_at": expires_at, "session_expires_at": time.time() + 8 * 3600, "renewals": 0,
        "chat": [], "chat_lock": threading.Lock(),
    }
    session.clear()
    session["sid"] = sid
    session["csrf"] = secrets.token_urlsafe(24)
    log.info("signed in %s; tokens stored server-side", token["userinfo"].get("name"))
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    STORE.pop(session.pop("sid", ""), None)
    session.clear()
    return redirect(url_for("index"))


@app.get("/logout/idp")
def logout_idp():
    STORE.pop(session.pop("sid", ""), None)
    session.clear()
    try:
        endpoint = oauth.idp.load_server_metadata().get("end_session_endpoint")
        if not endpoint:
            raise AuthUnavailable("Identity provider has no sign-out endpoint.")
    except (requests.RequestException, AuthUnavailable) as exc:
        log.error("Identity-provider logout unavailable: %s", type(exc).__name__)
        return login_error("Signed out of this app, but identity-provider sign-out is unavailable.", 502)
    return redirect(endpoint + "?" + urlencode({
        "post_logout_redirect_uri": f"{config.WEB_URL}/", "client_id": config.CLIENT_ID,
    }))


@app.post("/chat")
def chat_turn():
    entry = current_entry()
    if not entry:
        return jsonify(error="login_required", message="Your session has ended. Sign in again."), 401
    if not csrf_ok():
        return jsonify(error="csrf", message="This page is out of date. Reload it and try again."), 403
    payload = request.get_json(silent=True)
    question = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        return jsonify(error="invalid", message=f"Send a question of 1 to {MAX_QUESTION_CHARS} characters."), 400
    if not entry["chat_lock"].acquire(blocking=False):
        return jsonify(error="busy", message="Still answering your previous question."), 409
    question = question.strip()
    try:
        mcp = chat.McpSession(config.MCP_URL, lambda force: access_token_for(entry, force))
        turn = chat.run_turn(question, entry["chat"], user=entry["user"], mcp=mcp, model=MODEL)
        entry["chat"] += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": turn.answer, "engine": turn.engine, "trace": turn.trace,
             "renewed": mcp.renewed, "error": turn.error},
        ]
        del entry["chat"][:-MAX_HISTORY_ITEMS]
    except NeedsLogin:
        log.info("Interactive sign-in required after token renewal was rejected")
        STORE.pop(session.pop("sid", ""), None)
        return jsonify(error="login_required", message="Your identity provider needs you to sign in again."), 401
    except chat.ModelUnavailable as exc:
        return jsonify(error="model_unavailable", message=str(exc)), 502
    except AuthUnavailable as exc:
        log.error("%s", exc)
        return jsonify(error="auth_unavailable", message=str(exc)), 502
    except chat.McpError as exc:
        log.error("MCP request failed: HTTP %s %s", exc.status, exc.error)
        return jsonify(error="mcp_unavailable", message=f"The inventory service failed (HTTP {exc.status})."), 502
    except requests.RequestException as exc:
        log.error("Identity provider or MCP request failed: %s", type(exc).__name__)
        return jsonify(error="unavailable",
                       message="The identity provider or inventory service is unreachable; try again."), 502
    finally:
        entry["chat_lock"].release()
    log.info(json.dumps({"event": "chat_turn", "engine": turn.engine, "tool_calls": len(turn.trace),
                         "renewed": mcp.renewed}))
    return jsonify(answer=turn.answer, engine=turn.engine, trace=turn.trace, error=turn.error,
                   renewed=mcp.renewed, token=display_claims(entry["access_token"]))


@app.post("/chat/reset")
def chat_reset():
    entry = current_entry()
    if not entry:
        return jsonify(error="login_required", message="Your session has ended. Sign in again."), 401
    if not csrf_ok():
        return jsonify(error="csrf", message="This page is out of date. Reload it and try again."), 403
    if not entry["chat_lock"].acquire(blocking=False):
        return jsonify(error="busy", message="Still answering your previous question."), 409
    try:
        entry["chat"].clear()
    finally:
        entry["chat_lock"].release()
    return jsonify(ok=True)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inventory chat</title>
{% if user %}<meta name="csrf-token" content="{{ csrf }}">{% endif %}
<style nonce="{{ g.nonce }}">
 :root{--blue:#0f6cbd;--line:#e0e0e0;--muted:#616161}
 *{box-sizing:border-box}
 body{margin:0;height:100vh;display:flex;flex-direction:column;font:15px/1.45 "Segoe UI",system-ui,sans-serif;
   color:#1b1b1b;background:#f6f7f9}
 a{color:var(--blue)}
 header{display:flex;align-items:center;gap:16px;padding:10px 20px;background:#fff;border-bottom:1px solid var(--line)}
 header h1{flex:1;margin:0;font-size:17px}
 .who{font-size:13px;color:var(--muted);text-align:right}
 .who b{color:#1b1b1b}
 main{flex:1;overflow-y:auto;padding:20px}
 .wrap{max-width:860px;margin:0 auto}
 .msg{display:flex;margin:12px 0}
 .msg.user{justify-content:flex-end}
 .bubble{max-width:82%;padding:10px 14px;border-radius:14px;background:#fff;border:1px solid var(--line);
   white-space:pre-wrap;overflow-wrap:anywhere}
 .user .bubble{background:var(--blue);border-color:var(--blue);color:#fff;border-bottom-right-radius:4px}
 .assistant .bubble{border-bottom-left-radius:4px}
 .bubble.error{background:#fdf3f4;border-color:#eeacb2}
 .bubble.pending{color:var(--muted);font-style:italic}
 .meta{margin-top:8px;padding-top:6px;border-top:1px solid var(--line);font-size:12px;color:var(--muted);
   white-space:normal}
 .meta summary{cursor:pointer}
 .meta pre,.bar pre{margin:6px 0 0;padding:8px;background:#f3f3f3;border-radius:6px;white-space:pre-wrap;color:#333}
 .empty{margin-top:10vh;text-align:center;color:var(--muted)}
 .chips{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:14px}
 footer{padding:12px 20px 14px;background:#fff;border-top:1px solid var(--line)}
 form{display:flex;gap:8px;align-items:flex-end}
 textarea{flex:1;min-height:46px;max-height:220px;padding:11px 12px;font:inherit;border:1px solid #bdbdbd;
   border-radius:10px;resize:none}
 button,.btn{padding:9px 16px;font:inherit;color:#1b1b1b;background:#fff;border:1px solid #bdbdbd;border-radius:8px;
   cursor:pointer;text-decoration:none;display:inline-block}
 .primary{background:var(--blue);border-color:var(--blue);color:#fff}
 button:disabled{opacity:.5;cursor:default}
 .chip{padding:6px 12px;font-size:13px;border-radius:16px;max-width:380px;text-align:left}
 .bar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:8px;font-size:12px;color:var(--muted)}
 .bar select{max-width:430px;padding:4px;font:inherit}
 .bar button{padding:4px 10px;font-size:12px}
 .bar details{flex-basis:100%}
 .bar pre{max-height:180px;overflow:auto}
 .signin{max-width:540px;margin:12vh auto;padding:28px 32px;text-align:center;background:#fff;
   border:1px solid var(--line);border-radius:12px}
 .signin p{color:#424242}
</style></head><body>
{% if not user %}
<main><div class="signin">
 <h1>Inventory chat</h1>
 <p>Sign in once with {{ 'the offline mock provider (alice / demo)' if use_mock else 'your Microsoft work account' }},
  then ask questions about inventory.</p>
 <p>Answers come from {{ engine }}. Inventory lookups call the MCP server <b>as you</b> with a short-lived
  delegated token that renews silently; no API key is involved.</p>
 <a class="btn primary" href="/login">Sign in</a>
</div></main>
{% else %}
<header>
 <h1>Inventory chat</h1>
 <div class="who">Signed in as <b>{{ user.name }}</b> ({{ user.preferred_username }})<br>
  <a href="/logout">Sign out of this app</a> &middot;
  <a href="/logout/idp">{{ 'Sign out of mock IdP' if use_mock else 'Sign out of Microsoft' }}</a></div>
</header>
<main id="scroller"><div class="wrap" id="log">
 <div class="empty" id="empty">
  <p>Ask about inventory. Lookups run as {{ user.name }} through the MCP server; the data is fictional.</p>
  <div class="chips">{% for text in suggestions %}<button class="chip" type="button" data-q="{{ text }}">{{ text }}</button>{% endfor %}</div>
 </div>
</div></main>
<footer><div class="wrap">
 <form id="composer">
  <textarea id="message" rows="1" maxlength="{{ max_chars }}" autofocus
   placeholder="Ask a question (Enter to send, Shift+Enter for a new line)"></textarea>
  <button class="primary" id="send" type="submit">Send</button>
 </form>
 <div class="bar">
  {% if questions %}<select id="samples" aria-label="Test questions"><option value="">Insert a test question\u2026</option>
   {% for q in questions %}<option value="{{ loop.index0 }}">{{ q.label }}</option>{% endfor %}</select>{% endif %}
  <button type="button" id="reset">New chat</button>
  <span>{{ engine }} &middot; inventory token <span id="ttl">-</span>, renews automatically</span>
  <details><summary>Delegated access token (decoded for display)</summary>
   <pre id="claims">{{ claims | tojson(indent=2) }}</pre></details>
 </div>
</div></footer>
<script type="application/json" id="history-data">{{ history | tojson }}</script>
<script type="application/json" id="question-data">{{ questions | map(attribute="text") | list | tojson }}</script>
<script nonce="{{ g.nonce }}">
 const csrf = document.querySelector('meta[name="csrf-token"]').content;
 const log = document.getElementById('log'), scroller = document.getElementById('scroller');
 const form = document.getElementById('composer'), input = document.getElementById('message');
 const send = document.getElementById('send'), ttl = document.getElementById('ttl');
 const questions = JSON.parse(document.getElementById('question-data').textContent);
 let exp = {{ claims.exp | tojson }};

 function el(tag, cls, text) {
   const node = document.createElement(tag);
   if (cls) node.className = cls;
   if (text !== undefined) node.textContent = text;
   return node;
 }
 function add(role, text, cls) {
   const empty = document.getElementById('empty');
   if (empty) empty.remove();
   const row = el('div', 'msg ' + role), bubble = el('div', 'bubble' + (cls ? ' ' + cls : ''), text);
   row.appendChild(bubble);
   log.appendChild(row);
   scroller.scrollTop = scroller.scrollHeight;
   return bubble;
 }
 function describe(step) {
   const args = Object.entries(step.arguments || {}).map(([k, v]) => k + '=' + JSON.stringify(v)).join(', ');
   let text = (step.ok ? '\u2713 ' : '\u2717 ') + step.tool + '(' + args + ')';
   if (step.status && step.status !== 200) text += ' HTTP ' + step.status;
   if (step.ok && step.result && step.result.requested_by) text += ' as ' + step.result.requested_by;
   else if (!step.ok && step.detail) text += ': ' + String(step.detail).slice(0, 160);
   return text;
 }
 function annotate(bubble, data) {
   const steps = data.trace || [];
   const parts = steps.length ? steps.map(describe) : ['no tool call'];
   if (data.renewed) parts.push('token renewed silently');
   parts.push(data.engine);
   const meta = el('details', 'meta');
   meta.appendChild(el('summary', '', parts.join(' \u00b7 ')));
   meta.appendChild(el('pre', '', JSON.stringify(steps, null, 2)));
   bubble.appendChild(meta);
 }
 function tick() {
   const left = Math.max(0, exp - Math.floor(Date.now() / 1000));
   ttl.textContent = left > 120 ? Math.floor(left / 60) + ' min left' : left + 's left';
 }
 function resize() {
   input.style.height = 'auto';
   input.style.height = Math.min(input.scrollHeight, 220) + 'px';
 }
 async function ask(text) {
   text = text.trim();
   if (!text || send.disabled) return;
   add('user', text);
   input.value = '';
   resize();
   const pending = add('assistant', 'Thinking\u2026', 'pending');
   send.disabled = true;
   try {
     const r = await fetch('/chat', {method: 'POST', body: JSON.stringify({message: text}),
       headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}});
     let data;
     try { data = await r.json(); } catch (e) { data = {message: 'Unexpected response (HTTP ' + r.status + ').'}; }
     pending.className = 'bubble';
     if (!r.ok) {
       pending.classList.add('error');
       pending.textContent = data.message || 'Request failed (HTTP ' + r.status + ').';
       if (r.status === 401) {
         const link = el('a', '', ' Sign in again');
         link.href = '/login';
         pending.appendChild(link);
       }
       return;
     }
     pending.textContent = data.answer;
     if (data.error) pending.classList.add('error');
     annotate(pending, data);
     if (data.token) {
       exp = data.token.exp;
       tick();
       document.getElementById('claims').textContent = JSON.stringify(data.token, null, 2);
     }
   } catch (e) {
     pending.className = 'bubble error';
     pending.textContent = 'Network error: ' + e.message;
   } finally {
     send.disabled = false;
     input.focus();
     scroller.scrollTop = scroller.scrollHeight;
   }
 }

 for (const item of JSON.parse(document.getElementById('history-data').textContent)) {
   const bubble = add(item.role, item.content, item.error ? 'error' : '');
   if (item.role === 'assistant') annotate(bubble, item);
 }
 form.addEventListener('submit', (e) => { e.preventDefault(); ask(input.value); });
 input.addEventListener('keydown', (e) => {
   if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
 });
 input.addEventListener('input', resize);
 document.querySelectorAll('.chip').forEach((b) => b.addEventListener('click', () => ask(b.dataset.q)));
 const samples = document.getElementById('samples');
 if (samples) samples.addEventListener('change', () => {
   if (samples.value !== '') {
     input.value = questions[Number(samples.value)];
     resize();
     input.focus();
   }
   samples.value = '';
 });
 document.getElementById('reset').addEventListener('click', async () => {
   const r = await fetch('/chat/reset', {method: 'POST', headers: {'X-CSRF-Token': csrf}});
   if (r.ok || r.status === 401) location.reload();
 });
 setInterval(tick, 1000);
 tick();
</script>
{% endif %}
</body></html>
"""


def main():
    app.run(host=config.HOST, port=config.WEB_PORT)


if __name__ == "__main__":
    main()
