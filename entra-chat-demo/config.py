"""Explicit mock and single-tenant Entra settings; run_entra.ps1 loads the live configuration."""
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} for Entra mode; use run_entra.ps1.")
    return value


def endpoint(name: str, default: str) -> str:
    value = os.getenv(name, default).rstrip("/")
    parsed = urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not local)):
        raise ValueError(f"{name} must use HTTPS, except for loopback HTTP.")
    return value


HOST = "127.0.0.1"
IDP_PORT = int(os.getenv("IDP_PORT", "9000"))
MCP_PORT = int(os.getenv("MCP_PORT", "9001"))
WEB_PORT = int(os.getenv("WEB_PORT", "9002"))
# serve.py only: where the web app listens. The MCP resource server always stays on HOST.
WEB_BIND_HOST = str(ipaddress.ip_address(os.getenv("WEB_BIND_HOST") or HOST))
AUTH_MODE = os.getenv("AUTH_MODE", "mock")
if AUTH_MODE not in ("mock", "entra"):
    raise ValueError("AUTH_MODE must be mock or entra.")
USE_MOCK = AUTH_MODE == "mock"

WEB_URL = endpoint("WEB_URL", f"http://{HOST}:{WEB_PORT}")
REDIRECT_URI = f"{WEB_URL}/auth/callback"

if USE_MOCK:
    TENANT_ID = None
    ISSUER = f"http://{HOST}:{IDP_PORT}"
    CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "inventory-web")
    CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "demo-client-secret")
    API_AUDIENCE = os.getenv("API_AUDIENCE", "api://inventory-mcp")
else:
    TENANT_ID = str(UUID(required("ENTRA_TENANT_ID")))
    ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
    CLIENT_ID = str(UUID(required("OIDC_CLIENT_ID")))
    CLIENT_SECRET = required("OIDC_CLIENT_SECRET")
    API_AUDIENCE = str(UUID(required("API_AUDIENCE")))
if os.getenv("OIDC_ISSUER", ISSUER) != ISSUER:
    raise ValueError("OIDC_ISSUER does not match AUTH_MODE and ENTRA_TENANT_ID.")
DISCOVERY_URL = os.getenv("OIDC_DISCOVERY_URL", f"{ISSUER}/.well-known/openid-configuration")
if DISCOVERY_URL != f"{ISSUER}/.well-known/openid-configuration":
    raise ValueError("OIDC_DISCOVERY_URL must belong to the configured issuer.")

API_SCOPE = os.getenv("API_SCOPE", "inventory.read" if USE_MOCK else f"api://{API_AUDIENCE}/inventory.read")
LOGIN_SCOPES = f"openid profile offline_access {API_SCOPE}"

MCP_PUBLIC_URL = endpoint("MCP_PUBLIC_URL", f"http://{HOST}:{MCP_PORT}")
MCP_URL = endpoint("MCP_URL", f"{MCP_PUBLIC_URL}/mcp")
REQUIRED_SCOPE = os.getenv("REQUIRED_SCOPE", "inventory.read")
REQUIRED_ROLE = os.getenv("REQUIRED_ROLE", "Inventory.Reader")

# Mock IdP only: short lifetime so silent renewal is visible.
ACCESS_TOKEN_TTL = int(os.getenv("ACCESS_TOKEN_TTL", "60"))

# Optional chat model, called with this app's Azure identity. Without it an offline responder answers.
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
MODEL_REASONING_EFFORT = os.getenv("MODEL_REASONING_EFFORT", "low")
MODEL_TENANT_ID = os.getenv("AZURE_OPENAI_TENANT_ID") or TENANT_ID
if AZURE_OPENAI_ENDPOINT:
    model_url = urlsplit(AZURE_OPENAI_ENDPOINT)
    if (model_url.scheme != "https" or model_url.path or model_url.query or model_url.fragment
            or model_url.username or model_url.password or model_url.port
            or not (model_url.hostname or "").endswith(
                (".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com"))):
        raise ValueError("AZURE_OPENAI_ENDPOINT must be a resource endpoint such as https://<name>.openai.azure.com")
if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", AZURE_OPENAI_DEPLOYMENT):
    raise ValueError("AZURE_OPENAI_DEPLOYMENT is not a valid deployment name.")
if MODEL_REASONING_EFFORT not in ("minimal", "low", "medium", "high"):
    raise ValueError("MODEL_REASONING_EFFORT must be minimal, low, medium, or high.")
if MODEL_TENANT_ID:
    MODEL_TENANT_ID = str(UUID(MODEL_TENANT_ID))
# When set, the model is called with this user-assigned managed identity instead of the Azure CLI login.
MODEL_IDENTITY_CLIENT_ID = os.getenv("MODEL_IDENTITY_CLIENT_ID", "").strip() or None
if MODEL_IDENTITY_CLIENT_ID:
    MODEL_IDENTITY_CLIENT_ID = str(UUID(MODEL_IDENTITY_CLIENT_ID))

# Optional JSONL file whose string "query" fields populate the UI's test-question picker.
TEST_QUESTIONS_FILE = os.getenv("TEST_QUESTIONS_FILE", str(
    Path(__file__).resolve().parent.parent / "foundry-project" / "evals" / "datasets"
    / "aws-inventory-agent-turn-tests.jsonl"))
