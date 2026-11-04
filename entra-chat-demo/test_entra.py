"""Local regression checks for Entra configuration and OAuth boundaries; no tenant calls."""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

import config
import mcp_server
import run_demo
import serve
import webapp

TENANT = "11111111-1111-4111-8111-111111111111"
API = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


class EntraResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.settings = patch.multiple(
            config, USE_MOCK=False, TENANT_ID=TENANT, ISSUER=ISSUER, CLIENT_ID=CLIENT,
            API_AUDIENCE=API, API_SCOPE=f"api://{API}/inventory.read",
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        key_client = Mock()
        key_client.get_signing_key_from_jwt.return_value.key = self.key.public_key()
        self.keys = patch.object(mcp_server, "_signing_keys", return_value=key_client)
        self.keys.start()
        self.addCleanup(self.keys.stop)
        self.client = mcp_server.app.test_client()

    def call(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": API, "sub": "pairwise-subject", "oid": USER, "tid": TENANT,
            "ver": "2.0", "azp": CLIENT, "scp": "inventory.read", "roles": ["Inventory.Reader"],
            "iat": now, "nbf": now - 1, "exp": now + 60,
        }
        claims.update(overrides)
        token = jwt.encode(claims, self.key, algorithm="RS256")
        return self.client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_inventory", "arguments": {"sku": "WIDGET-001"}},
        })

    def test_delegated_token_preserves_tenant_and_user(self):
        response = self.call()
        self.assertEqual(response.status_code, 200)
        result = response.json["result"]["structuredContent"]
        self.assertEqual(result["quantity"], 42)
        self.assertEqual(result["principal"], {"tenant_id": TENANT, "object_id": USER})

    def test_invalid_identity_is_rejected(self):
        for claims in (
            {"aud": CLIENT}, {"iss": "https://invalid.example"}, {"tid": CLIENT},
            {"azp": API}, {"ver": "1.0"}, {"oid": None}, {"exp": int(time.time()) - 60},
        ):
            with self.subTest(claims=claims):
                self.assertEqual(self.call(**claims).status_code, 401)

    def test_delegated_scope_and_exact_role_required(self):
        for claims in ({"scp": None}, {"scp": "other.scope"}, {"roles": []},
                       {"roles": "Inventory.Reader"}, {"roles": ["Not.Inventory.Reader"]}):
            with self.subTest(claims=claims):
                self.assertEqual(self.call(**claims).status_code, 403)

    def test_discovery_advertises_full_entra_scope(self):
        response = self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(response.json["scopes_supported"], [f"api://{API}/inventory.read"])


class WebTokenTests(unittest.TestCase):
    def setUp(self):
        self.entry = {
            "access_token": "old-test-token", "refresh_token": "old-test-refresh",
            "expires_at": time.time() + 3600, "session_expires_at": time.time() + 3600, "renewals": 0,
        }

    def renew(self, status, body):
        response = Mock(status_code=status)
        response.json.return_value = body
        with patch.object(webapp.oauth.idp, "load_server_metadata",
                          return_value={"token_endpoint": f"{ISSUER}/token"}), \
                patch.object(webapp.requests, "post", return_value=response) as post:
            token, renewed = webapp.access_token_for(self.entry, force_refresh=True)
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
            return token, renewed

    def test_forced_refresh_rotates_server_side_tokens(self):
        token, renewed = self.renew(200, {
            "access_token": "new-test-token", "refresh_token": "new-test-refresh",
            "token_type": "Bearer", "expires_in": 3600,
        })
        self.assertEqual(token, "new-test-token")
        self.assertTrue(renewed)
        self.assertEqual(self.entry["refresh_token"], "new-test-refresh")
        self.assertEqual(self.entry["renewals"], 1)

    def test_provider_outage_keeps_the_refresh_token(self):
        with self.assertRaises(webapp.AuthUnavailable):
            self.renew(503, {"error": "temporarily_unavailable"})
        self.assertEqual(self.entry["refresh_token"], "old-test-refresh")
        self.assertEqual(self.entry["renewals"], 0)

    def test_revocation_requires_interactive_login(self):
        with self.assertRaises(webapp.NeedsLogin):
            self.renew(400, {"error": "invalid_grant"})

    def test_malformed_success_is_not_accepted(self):
        for body in (
            {"access_token": "", "token_type": "Bearer", "expires_in": 3600},
            {"access_token": "new", "token_type": "Bearer", "expires_in": 0},
            {"access_token": "new", "token_type": "MAC", "expires_in": 3600},
        ):
            with self.subTest(body=body), self.assertRaises(webapp.AuthUnavailable):
                self.renew(200, body)
            self.assertEqual(self.entry["access_token"], "old-test-token")

    def test_microsoft_logout_uses_discovery_and_clears_local_session(self):
        client = webapp.app.test_client()
        with client.session_transaction(base_url=config.WEB_URL) as session:
            session["sid"] = "test-logout"
        webapp.STORE["test-logout"] = self.entry
        endpoint = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/logout"
        with patch.object(webapp.oauth.idp, "load_server_metadata",
                          return_value={"end_session_endpoint": endpoint}):
            response = client.get("/logout/idp", base_url=config.WEB_URL)
        self.assertTrue(response.location.startswith(endpoint + "?"))
        self.assertEqual(parse_qs(urlsplit(response.location).query)["post_logout_redirect_uri"],
                         [config.WEB_URL + "/"])
        self.assertNotIn("test-logout", webapp.STORE)
        with client.session_transaction(base_url=config.WEB_URL) as session:
            self.assertNotIn("sid", session)

    def test_callback_rejects_missing_state(self):
        with patch.object(webapp.oauth.idp, "fetch_access_token") as fetch:
            response = webapp.app.test_client().get("/auth/callback?code=not-a-real-code",
                                                   base_url=config.WEB_URL)
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()


class ServeTests(unittest.TestCase):
    def test_mcp_stays_on_loopback_and_web_uses_the_bind_host(self):
        with patch.object(serve, "create_server") as create, patch.object(serve.threading, "Thread") as thread, \
                patch.object(serve, "serve") as run, patch.object(serve.signal, "signal") as handler, \
                patch.object(serve.logging, "basicConfig"), patch.object(config, "WEB_BIND_HOST", "0.0.0.0"):
            serve.main()
        self.assertEqual(config.HOST, "127.0.0.1")
        self.assertEqual((create.call_args.args, create.call_args.kwargs["host"], create.call_args.kwargs["port"]),
                         ((mcp_server.app,), "127.0.0.1", config.MCP_PORT))
        thread.return_value.start.assert_called_once()
        self.assertEqual((run.call_args.args, run.call_args.kwargs["host"], run.call_args.kwargs["port"]),
                         ((webapp.app,), "0.0.0.0", config.WEB_PORT))
        create.return_value.close.assert_called_once()
        self.assertIs(handler.call_args.args[1], serve.terminate)
        with self.assertRaises(SystemExit):
            serve.terminate()

    def test_waitress_serves_both_applications(self):
        with patch.object(config, "MCP_PORT", 0):
            mcp = serve.start_mcp()
        web = serve.create_server(webapp.app, host="127.0.0.1", port=0)
        threading.Thread(target=web.run, daemon=True).start()
        self.addCleanup(web.close)
        self.addCleanup(mcp.close)
        health = requests.get(f"http://127.0.0.1:{web.effective_port}/healthz", timeout=10)
        self.assertEqual((health.status_code, health.json()["authentication"]), (200, config.AUTH_MODE))
        self.assertEqual(requests.post(f"http://127.0.0.1:{mcp.effective_port}/mcp", json={}, timeout=10).status_code, 401)


class ConfigurationTests(unittest.TestCase):
    def test_entra_never_launches_mock_idp(self):
        with patch.object(config, "USE_MOCK", False), \
                patch.object(run_demo, "make_server") as server, \
                patch.object(run_demo.threading, "Thread"):
            self.assertEqual(len(run_demo.start_all()), 2)
        self.assertEqual([call.args[1] for call in server.call_args_list],
                         [config.MCP_PORT, config.WEB_PORT])

    def test_entra_has_no_fallback_client_secret(self):
        env = dict(os.environ, AUTH_MODE="entra", ENTRA_TENANT_ID=TENANT, OIDC_CLIENT_ID=CLIENT,
                   API_AUDIENCE=API, OIDC_CLIENT_SECRET="")
        result = subprocess.run(
            [sys.executable, "-c", "import config"], cwd=Path(__file__).parent,
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set OIDC_CLIENT_SECRET", result.stderr)


if __name__ == "__main__":
    unittest.main()
