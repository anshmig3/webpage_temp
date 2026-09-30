"""
Helpdesk chat backend — proxies the front-end chat widget to an Azure AI
Foundry Agent Service agent, called through its OpenAI-compatible
"Responses API" protocol endpoint:

    https://<resource>.services.ai.azure.com/api/projects/<project>/agents/<agent>/endpoint/protocols/openai/responses

Auth: this endpoint only supports Microsoft Entra ID bearer tokens — API
keys are NOT supported. This code uses DefaultAzureCredential, so make sure
you're signed in locally before starting the server, e.g.:

    az login

...and that your account (or service principal) has been granted the
"Foundry User" role (or an equivalent custom role with the
Microsoft.CognitiveServices/accounts/AIServices/applications/invoke/action
permission) on the Foundry resource.

Implementation note: this talks to the endpoint with plain HTTP (requests)
rather than the openai-python SDK, so the request/response shape and the
bearer-token refresh are fully explicit and easy to debug — it mirrors the
curl example in Microsoft's own docs for this endpoint.

Run:
    uvicorn main:app --reload --port 8000

Env vars (optional overrides):
    AZURE_FOUNDRY_RESOURCE     default: "first-test-01-resource"
    AZURE_FOUNDRY_PROJECT      default: "first-test-01"
    AZURE_FOUNDRY_AGENT        default: "supervisor-agent"
    AZURE_FOUNDRY_API_VERSION  default: "2025-11-15-preview"

Note on corporate networks: if you're behind a TLS-inspecting proxy or
endpoint-security product, `requests`' bundled CA list (certifi) won't
recognize the proxy's injected root certificate even though Windows does,
and calls below will fail with SSLCertVerificationError. `truststore`
(imported first, below) fixes this by making Python's ssl module verify
against the OS certificate store instead of certifi's bundle.
"""

import json
import os
import time
import logging

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    # Not installed — fine on machines without a TLS-inspecting proxy.
    # If you hit SSLCertVerificationError below, `pip install truststore`.
    pass

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.identity import AzureCliCredential, DefaultAzureCredential
from azure.core.exceptions import ClientAuthenticationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("helpdesk-backend")

RESOURCE = os.environ.get("AZURE_FOUNDRY_RESOURCE", "first-test-01-resource")
PROJECT = os.environ.get("AZURE_FOUNDRY_PROJECT", "first-test-01")
AGENT = os.environ.get("AZURE_FOUNDRY_AGENT", "supervisor-agent")
API_VERSION = os.environ.get("AZURE_FOUNDRY_API_VERSION", "2025-11-15-preview")
TOKEN_SCOPE = "https://ai.azure.com/.default"

# For local development: run the agent with `python main.py` in
# src/agent-framework-agent-basic-responses (it listens on http://localhost:8088),
# then point this backend at it instead of the deployed cloud agent, e.g.:
#   $env:AGENT_LOCAL_URL = "http://localhost:8088/responses"
# Editing main.py and restarting the local agent then takes effect immediately,
# with no redeploy to Foundry needed. The local host doesn't require (or check)
# an Entra bearer token, so auth is skipped whenever this is set.
LOCAL_URL = os.environ.get("AGENT_LOCAL_URL")

BASE_URL = (
    f"https://{RESOURCE}.services.ai.azure.com/api/projects/{PROJECT}"
    f"/agents/{AGENT}/endpoint/protocols/openai"
)
RESPONSES_URL = LOCAL_URL or f"{BASE_URL}/responses"

app = FastAPI(title="Helpdesk Chat Backend")

# Allow the mockup front end (served from file:// or any localhost port)
# to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Go straight to the Azure CLI credential (you're authenticating via
# `az login`) instead of DefaultAzureCredential's full probe chain — that
# chain checks environment vars, managed identity (which has to time out a
# network probe on a non-Azure machine), workload identity, VS Code, shared
# token cache, etc. before it ever gets to the CLI credential, adding a real
# delay on the first request. Falls back to DefaultAzureCredential only if
# the CLI credential itself doesn't work in your environment.
_cached_token: str | None = None
_cached_token_expiry: float = 0.0

try:
    _credential = AzureCliCredential()
    _startup_token = _credential.get_token(TOKEN_SCOPE)  # fail fast at import time, not mid-request
    # Cache it now so the first /api/chat request doesn't fetch it again.
    _cached_token = _startup_token.token
    _cached_token_expiry = _startup_token.expires_on
except Exception:
    logger.info("AzureCliCredential unavailable, falling back to DefaultAzureCredential")
    _credential = DefaultAzureCredential()

# Reuse one connection (keep-alive) across requests instead of letting
# `requests` open a fresh TCP+TLS handshake for every chat message — this
# matters more than usual here since a corporate TLS-inspecting proxy adds
# its own overhead to every new handshake.
_session = requests.Session()


def get_access_token() -> str:
    """Return a cached Entra ID bearer token, refreshing it shortly before
    it expires."""
    global _cached_token, _cached_token_expiry

    now = time.time()
    if _cached_token and (_cached_token_expiry - now) > 60:
        return _cached_token

    try:
        token = _credential.get_token(TOKEN_SCOPE)
    except ClientAuthenticationError as exc:
        logger.exception("Azure authentication failed")
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not get an Azure access token. Make sure you're signed "
                f"in (e.g. `az login`) and have access to {TOKEN_SCOPE}. "
                f"Original error: {exc}"
            ),
        ) from exc

    _cached_token = token.token
    _cached_token_expiry = token.expires_on
    return _cached_token


def extract_output_text(data: dict) -> str:
    """Pull the assistant's reply text out of a Responses API JSON body,
    tolerating a couple of shape variations."""
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]

    texts: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    texts.append(part["text"])
    if texts:
        return "\n".join(texts)

    if isinstance(data.get("error"), dict) and data["error"].get("message"):
        return f"(agent error) {data['error']['message']}"

    logger.warning("Could not find output text in response: %s", data)
    return "(The agent responded, but no text content was found in its output.)"


def extract_tools_used(data: dict) -> list[str]:
    """Return the connected-agent tool names actually called this turn, in call order.

    The Responses API reports each tool invocation as a ``function_call`` output item
    with a ``name`` field, alongside the ``message`` items extract_output_text reads -
    this is the same information the OTel tool-call traces show, just already present
    in the response body without needing to dig through logs.
    """
    tools: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "function_call":
            name = item.get("name")
            if name and name not in tools:
                tools.append(name)
    return tools


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    email: str | None = None  # None/omitted => guest, set => email-authenticated


class ChatResponse(BaseModel):
    reply: str
    login_required: bool = False
    tools_used: list[str] = []


# Marker key the supervisor agent (src/agent-framework-agent-basic-responses/main.py)
# looks for on a leading `system` input item to learn the caller's login state. The
# Responses API protocol on this endpoint does not forward request-level metadata to
# the hosted agent, so login state has to travel as part of the message input instead.
SESSION_CONTEXT_MARKER = "__session_context__"

# Literal the supervisor agent replies with (and nothing else) when the caller's
# question needs a tool that isn't available to a guest. Kept as an exact sentinel
# rather than parsed from free text so this is reliable to detect. The friendly copy
# shown to the user lives here, not in the agent, so it's easy to change without a
# redeploy of the agent itself.
LOGIN_REQUIRED_SENTINEL = "LOGIN_REQUIRED"
LOGIN_REQUIRED_MESSAGE = "To help with that, please log in with your email below — I'll pick up right where we left off."


@app.get("/health")
def health():
    return {"status": "ok", "endpoint": RESPONSES_URL}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_context = {
        SESSION_CONTEXT_MARKER: True,
        "login_type": "email" if req.email else "guest",
    }
    if req.email:
        session_context["email"] = req.email

    # This agent endpoint's Responses API protocol is stateless — it does
    # not remember prior turns server-side — so resend the full
    # conversation as a list of input items every time, with the session
    # context marker leading so the supervisor agent's access-control
    # middleware can find it consistently.
    input_items = [{"role": "system", "content": json.dumps(session_context)}]
    input_items += [{"role": turn.role, "content": turn.content} for turn in req.history]
    input_items.append({"role": "user", "content": req.message})

    if LOCAL_URL:
        params = {}
        headers = {"Content-Type": "application/json"}
    else:
        params = {"api-version": API_VERSION}
        headers = {
            "Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json",
        }

    try:
        resp = _session.post(
            RESPONSES_URL,
            params=params,
            headers=headers,
            json={"input": input_items},
            # Some connected agents take ~20-60s to answer for real, and the model may
            # retry a slow/failed tool call once, so a turn can legitimately run past 60s.
            timeout=150,
        )
    except requests.RequestException as exc:
        logger.exception("Request to agent endpoint failed")
        raise HTTPException(status_code=502, detail=f"Could not reach agent endpoint: {exc}") from exc

    if not resp.ok:
        logger.error("Agent endpoint returned %s: %s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=502,
            detail=f"Agent endpoint returned {resp.status_code}: {resp.text[:500]}",
        )

    response_body = resp.json()
    reply = extract_output_text(response_body)
    tools_used = extract_tools_used(response_body)
    if reply.strip() == LOGIN_REQUIRED_SENTINEL:
        return ChatResponse(reply=LOGIN_REQUIRED_MESSAGE, login_required=True, tools_used=tools_used)
    return ChatResponse(reply=reply, tools_used=tools_used)