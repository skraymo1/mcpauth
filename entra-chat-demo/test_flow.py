"""End-to-end check of the sign-in-once chat flow against the mock IdP: python test_flow.py"""
import logging
import re
import sys

import requests

import chat
import config
import run_demo
import webapp

failures = []


def check(name: str, condition: bool, detail: str = ""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def post(browser: requests.Session, path: str, body: dict | None = None) -> requests.Response:
    """POST like the chat page does, with the CSRF token from the current page when signed in."""
    page = browser.get(f"{config.WEB_URL}/", timeout=10)
    csrf = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    headers = {"X-CSRF-Token": csrf.group(1)} if csrf else {}
    return browser.post(f"{config.WEB_URL}{path}", json=body, headers=headers, timeout=30)


def ask(browser: requests.Session, question: str) -> requests.Response:
    return post(browser, "/chat", {"message": question})


def mcp_ping(token: str | None) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.post(config.MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         headers=headers, timeout=10)


def sign_in(browser: requests.Session, username: str | None) -> str:
    """Start sign-in; type credentials only if the IdP asks for them. Returns the final page HTML."""
    page = browser.get(f"{config.WEB_URL}/login")
    if "Mock Entra sign-in" in page.text:
        assert username, "IdP unexpectedly prompted for credentials"
        query = re.search(r'name="query" value="([^"]*)"', page.text).group(1).replace("&amp;", "&")
        page = browser.post(f"{config.ISSUER}/login",
                            data={"username": username, "password": "demo", "query": query})
    return page.text


if __name__ == "__main__":
    if not config.USE_MOCK:
        sys.exit("test_flow.py exercises the mock only. Run it in a shell without the Entra environment.")
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    webapp.MODEL, webapp.ENGINE = None, chat.OFFLINE_ENGINE  # deterministic answers without Azure access
    servers = run_demo.start_all()
    try:
        browser = requests.Session()
        check("signed-out page offers Sign in", "Sign in" in browser.get(config.WEB_URL).text)

        html = sign_in(browser, "alice")
        check("alice signs in once (password prompt) and lands in the chat",
              "Alice Rivera" in html and 'id="composer"' in html)
        cookie = webapp.app.session_interface.get_signing_serializer(webapp.app).loads(
            browser.cookies["web_session"])
        check("tokens stay out of the browser cookie", set(cookie) == {"sid", "csrf"})
        entry = webapp.STORE[cookie["sid"]]
        old_access, old_refresh = entry["access_token"], entry["refresh_token"]
        blocked = browser.post(f"{config.WEB_URL}/chat", json={"message": "hi"}, timeout=10)
        check("chat requires the session CSRF token", blocked.status_code == 403)

        r = ask(browser, "How many WIDGET-001 do we have?")
        body = r.json()
        step = (body.get("trace") or [{}])[0]
        check("question is answered from the tool",
              r.status_code == 200 and "42" in body.get("answer", "") and "ATL-DEMO" in body["answer"], r.text)
        check("tool ran as the signed-in user",
              step.get("ok") is True and "alice@" in step["result"]["requested_by"], str(step))
        check("transcript survives a page reload",
              "How many WIDGET-001 do we have?" in browser.get(config.WEB_URL, timeout=10).text)

        entry["expires_at"] = 0
        r = ask(browser, "Check WIDGET-001 again")
        check("expired token is renewed silently mid-chat",
              r.status_code == 200 and r.json().get("renewed") is True, r.text)
        check("access and refresh tokens both rotate",
              entry["access_token"] != old_access and entry["refresh_token"] != old_refresh)

        r = ask(browser, "Is TEST-404 in stock?")
        check("unknown SKU is reported, not invented",
              r.status_code == 200 and "Unknown demo SKU" in r.json().get("answer", ""), r.text)

        r = post(browser, "/chat/reset")
        check("New chat clears the transcript", r.status_code == 200 and entry["chat"] == [])

        r = mcp_ping(None)
        check("MCP: no token -> 401 with discovery hint",
              r.status_code == 401 and "resource_metadata=" in r.headers.get("WWW-Authenticate", ""))
        meta = requests.get(f"{config.MCP_PUBLIC_URL}/.well-known/oauth-protected-resource", timeout=10).json()
        check("protected-resource metadata names the IdP", meta["authorization_servers"] == [config.ISSUER])
        r = mcp_ping(entry["id_token"])
        check("MCP: ID token (wrong audience) rejected",
              r.status_code == 401 and "InvalidAudience" in r.json().get("error_description", ""), r.text)
        check("MCP: garbage bearer token -> 401", mcp_ping("not.a.jwt").status_code == 401)

        browser.get(f"{config.WEB_URL}/logout")
        check("app sign-out ends chat access", ask(browser, "hi").status_code == 401)
        html = sign_in(browser, None)
        check("re-sign-in is silent (no password prompt)", "Alice Rivera" in html)

        browser.get(f"{config.WEB_URL}/logout/idp", timeout=10)
        check("provider sign-out also ends chat access", ask(browser, "hi").status_code == 401)
        html = sign_in(browser, "bob")
        check("bob signs in", "Bob Chen" in html)
        r = ask(browser, "How many WIDGET-001 do we have?")
        body = r.json()
        check("bob lacks the role -> 403 explained in the chat",
              r.status_code == 200 and body.get("error") is True and "Inventory.Reader" in body.get("answer", "")
              and body["trace"][0]["status"] == 403, r.text)
    finally:
        for server in servers:
            server.shutdown()
    print("\nAll checks passed." if not failures else f"\n{len(failures)} check(s) failed: {failures}")
    sys.exit(1 if failures else 0)
