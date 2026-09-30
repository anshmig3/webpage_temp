# Copyright (c) Microsoft. All rights reserved.

import json
import logging
import os
import socket
import time

try:
    import truststore

    # On corporate networks with a TLS-inspecting proxy, requests' bundled CA list
    # (certifi) won't recognize the proxy's injected root certificate even though
    # Windows does. This makes Python's ssl module verify against the OS certificate
    # store instead, matching agentic-website/backend/main.py's fix for the same issue.
    truststore.inject_into_ssl()
except ImportError:
    pass

import httpx
from agent_framework import Agent, AgentContext, AgentMiddleware, Content, FunctionTool, Message
from agent_framework.a2a import A2AAgent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import configure_otel_providers
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Marker key used by the chat backend (see agentic-website/backend/main.py) to pass the
# caller's login state to this agent as a leading `system` input item. The Foundry
# Responses protocol does not forward request-level metadata to hosted agents, so the
# login state has to travel as part of the message input instead.
SESSION_CONTEXT_MARKER = "__session_context__"

# Connected agents, reached over the A2A (Agent2Agent) protocol. URLs are read from
# environment variables so real endpoints can be swapped in without code changes.
CONNECTED_AGENTS = {
    "faq_agent": {
        "env_var": "FAQ_AGENT_URL",
        "description": (
            "Answers frequently-asked questions about policies and how the site "
            "works (returns, shipping timelines in general, account setup, etc). Available "
            "to every caller, including guests. Does NOT know product prices,  "
            "order's details, or a specific order's shipping/refund status - never call this "
            "for those, even if it's the only tool available."
        ),
    },
    "order_agent": {
        "env_var": "ORDER_AGENT_URL",
        "description": (
            "Looks up order status, order history, and order details for a signed-in "
            "customer's account. Requires the caller to be logged in with their email."
        ),
    },
    "panda_agent": {
        "env_var": "PANDA_AGENT_URL",
        "description": (
            "Product catalog and pricing agent: looks up current prices, availability, and "
            "product details for a given product ID or name. Requires the caller to be "
            "logged in with their email."
        ),
    },
    "shipping_refund_info_agent": {
        "env_var": "SHIPPING_REFUND_AGENT_URL",
        "description": (
            "Return and exchange policy agent: explains RMA requirements, return time "
            "limits, restocking/administrative fees, and warranty or core return rules. "
            "This is general policy information, not tied to any specific customer's "
            "account or order - available to every caller, including guests."
        ),
    },
}

# Agents whose answers are general/public content rather than tied to a specific
# customer's own account or order - these are accessible to guests. Everything else
# in CONNECTED_AGENTS is email-login-only.
GUEST_ACCESSIBLE_AGENTS = {"faq_agent", "shipping_refund_info_agent"}


class LoginAccessControlMiddleware(AgentMiddleware):
    """Restricts which connected agents are reachable based on the caller's login state.

    Guests (no verified email) can only reach ``faq_agent``. Callers who logged in with
    an email get every connected agent. The login state is read from a leading `system`
    message carrying a JSON payload of the shape
    ``{"__session_context__": true, "login_type": "guest" | "email", "email": "..."}``.
    That raw JSON marker is replaced with a plain-language system note instead of just
    being stripped: connected-agent descriptions say things like "requires login," and
    without an explicit statement of the *current* caller's login state, the model has
    no way to know that requirement is already satisfied - it was calling LOGIN_REQUIRED
    even when the right tool was right there in its tool list, because it couldn't tell
    whether the caller in front of it was actually logged in.
    """

    def __init__(self, guest_tools: list[FunctionTool], full_tools: list[FunctionTool]) -> None:
        self._guest_tools = guest_tools
        self._full_tools = full_tools

    async def process(self, context: AgentContext, call_next) -> None:  # type: ignore[no-untyped-def]
        login_type = "guest"
        email: str | None = None

        for index, message in enumerate(context.messages):
            if message.role != "system" or not message.text:
                continue
            try:
                payload = json.loads(message.text)
            except ValueError:
                continue
            if not isinstance(payload, dict) or not payload.get(SESSION_CONTEXT_MARKER):
                continue
            if payload.get("login_type") == "email" and payload.get("email"):
                login_type = "email"
                email = payload["email"]
            context.messages[index] = Message(
                role="system",
                contents=[Content.from_text(_login_state_note(login_type, email))],
            )
            break

        context.tools = self._full_tools if login_type == "email" else self._guest_tools
        context.metadata["login_type"] = login_type
        logger.info("Resolved caller login_type=%s; tools=%s", login_type, [t.name for t in context.tools])

        await call_next()


def _login_state_note(login_type: str, email: str | None) -> str:
    if login_type == "email":
        return (
            f"The current caller is already logged in with the email address {email}. The "
            "tools listed as available to you this turn are already the ones this caller is "
            "authorized to use - if a tool's own description mentions requiring login, that "
            "requirement is already satisfied for this caller. Do not ask them to log in "
            "again, and do not reply with LOGIN_REQUIRED. If a tool needs the caller's own "
            f"email to look up their account or orders, include {email} in what you send it - "
            "the caller should never be asked to repeat their own email back to you."
        )
    return (
        "The current caller is a guest and is not logged in. Only the tools listed as "
        "available to you this turn may be used - if the topic needs a different tool, "
        "follow the LOGIN_REQUIRED instruction."
    )


class _EntraBearerAuth(httpx.Auth):
    """Attaches a cached Microsoft Entra ID bearer token to every request.

    Foundry-hosted agent endpoints (Responses and A2A alike) only accept Entra ID
    bearer tokens, not API keys, and a plain A2AAgent(url=...) sends no auth at all.
    A2AAgent's own auth_interceptor only fires when the target's AgentCard advertises
    a security scheme, which these endpoints don't reliably do, so the header is
    attached unconditionally here instead. Mirrors the token caching in
    agentic-website/backend/main.py.
    """

    _TOKEN_SCOPE = "https://ai.azure.com/.default"

    def __init__(self, credential: TokenCredential) -> None:
        self._credential = credential
        self._token: str | None = None
        self._expiry: float = 0.0

    def _get_token(self) -> str:
        now = time.time()
        if self._token is None or (self._expiry - now) < 60:
            token = self._credential.get_token(self._TOKEN_SCOPE)
            self._token = token.token
            self._expiry = token.expires_on
        return self._token

    def auth_flow(self, request: httpx.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


# Some connected agents (e.g. product-pricing-agent) are themselves slow to respond
# (observed ~18s for a real answer). A2AAgent's own `timeout=` kwarg only takes effect
# when it builds its own httpx client; since we always supply our own (to attach the
# Entra auth), the timeout has to be set here instead, or it silently falls back to
# httpx's ~5s default and every slow-but-legitimate answer looks like a dead agent.
CONNECTED_AGENT_TIMEOUT_SECONDS = 60.0


def _build_connected_agent_tools(credential: TokenCredential) -> dict[str, FunctionTool]:
    """Connect to every configured agent over A2A and wrap each as a callable tool."""
    tools: dict[str, FunctionTool] = {}
    for name, spec in CONNECTED_AGENTS.items():
        url = os.environ.get(spec["env_var"])
        if not url:
            logger.warning(
                "%s is not set; the '%s' connected agent will not be available.", spec["env_var"], name
            )
            continue
        http_client = httpx.AsyncClient(auth=_EntraBearerAuth(credential), timeout=CONNECTED_AGENT_TIMEOUT_SECONDS)
        remote_agent = A2AAgent(name=name, description=spec["description"], url=url, http_client=http_client)
        tools[name] = remote_agent.as_tool(name=name, description=spec["description"])
    return tools


def _vs_code_extension_port_if_listening(port: int, host: str = "127.0.0.1") -> int | None:
    """Return `port` only if something is actually listening there.

    The AI Toolkit / Microsoft Foundry VS Code extension opens an OTLP receiver on this
    port when it's running. Outside that extension (e.g. plain `python main.py`), nothing
    listens there, so passing the port unconditionally makes every trace/metric/log export
    retry and fail with "Connection refused" noise.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return port if sock.connect_ex((host, port)) == 0 else None


def main() -> None:
    configure_otel_providers(
        vs_code_extension_port=_vs_code_extension_port_if_listening(4317),
        enable_sensitive_data=False,
    )
    credential = DefaultAzureCredential()
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=credential,
    )

    connected_tools = _build_connected_agent_tools(credential)
    guest_tools = [tool for name, tool in connected_tools.items() if name in GUEST_ACCESSIBLE_AGENTS]
    full_tools = list(connected_tools.values())

    agent = Agent(
        client=client,
        name="supervisor-agent",
        instructions=(
            "You are the supervisor agent for the helpdesk, talking directly to a customer. "
            "Be to the point: short answers, no unnecessary questions.\n\n"
            "Every message from the customer is exactly one of two things - decide which "
            "first, before anything else:\n"
            "A. Pure greeting or small talk (e.g. \"hi\", \"thanks\", \"how are you\") with no "
            "actual request in it. Reply with one brief, plain sentence. Do not list what you "
            "can help with, do not ask 'which would you like help with', do not call a tool.\n"
            "B. A request to DO something none of your tools can actually do - place a new "
            "order, buy/purchase something, modify or cancel an order, or start a return/"
            "exchange. Every connected tool only looks up existing information (prices, "
            "availability, order status/history, general policy) - none of them can take an "
            "action like this, and logging in would not change that. Do not call any tool as a "
            "substitute (e.g. do not pull up pricing or a catalog just because the request "
            "mentioned a product), and do not reply LOGIN_REQUIRED. Instead, in one or two "
            "short sentences, say plainly that you can't complete that action yourself, and "
            "mention what you can help with instead (checking a product's price/availability, "
            "or general policy info) - direct them to the site's normal purchase/checkout flow "
            "for the action itself.\n"
            "C. Anything else - any question, no matter how small, generic, or seemingly common-"
            "knowledge it is (this includes things like part numbers, typical prices, standard "
            "policies or procedures - you do not actually know any of that, you only look up a "
            "customer's real answer through a tool). For every case C message, follow rules 1-5 "
            "below. There is no other option of answering it yourself from general knowledge.\n\n"
            "Rules for case C:\n"
            "1. Pick the one connected tool whose description matches the topic (price, order "
            "status, shipping, refunds, general FAQ, etc). Only tools listed as available to you "
            "this turn exist - if none of them matches the topic, do not call any tool, including "
            "the closest-sounding one. A tool being available to you this turn means this caller "
            "is already authorized to use it right now, even if the tool's own description "
            "mentions login - do not re-check or second-guess that.\n"
            "2. If no available tool matches the topic: reply with EXACTLY the literal text "
            "LOGIN_REQUIRED and nothing else - no punctuation, no apology, no explanation, no "
            "tool/agent names. The application (not you) will show the customer a login prompt "
            "and re-ask your original question once they've logged in, so your entire response "
            "must be just that one word.\n"
            "3. If a matching tool is available: call it immediately using only what the "
            "customer already said (e.g. a product ID or order number). Do not ask the customer "
            "about currency, region, tax, units, or any other detail before calling the tool - "
            "the tool itself will ask if it truly needs more, and only then should you relay "
            "that specific question to the customer.\n"
            "4. Your reply must be built only from what the tool actually returned. Never add, "
            "expand on, or fill in specifics (steps, policies, numbers, SKUs, links, statuses) "
            "that the tool's result didn't literally contain, even if they sound plausible or "
            "you're confident you already know the answer - you have no real information beyond "
            "the tool result, so anything else, however small, is a fabrication.\n"
            "5. If the tool's result doesn't actually answer the customer's question (e.g. it's "
            "empty, generic, or unrelated), say plainly that you don't have that information "
            "right now - do not complete or improve on its answer yourself.\n\n"
            "Reminder on rule 2, since this is the one rule you are most likely to drift from: "
            "when no available tool matches the topic, your ENTIRE response must be the exact "
            "six characters LOGIN_REQUIRED - never a sentence that explains you can't help, "
            "never a request for an order ID/email/phone, never a suggestion to check the "
            "website yourself. If you notice yourself composing an apology or asking the "
            "customer for an identifier, that is the signal you were about to break this rule - "
            "discard that draft and output only LOGIN_REQUIRED instead."
        ),
        middleware=[LoginAccessControlMiddleware(guest_tools=guest_tools, full_tools=full_tools)],
        # History will be managed by the hosting infrastructure, thus there
        # is no need to store history by the service. Learn more at:
        # https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
