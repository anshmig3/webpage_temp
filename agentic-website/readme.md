# AeroParts Distribution — Mockup + Helpdesk Chat (Azure AI Foundry Agent)

## What's here

```
aeroparts-mockup/
├── index.html          ← the front-end page + floating chat widget (open directly in a browser)
└── backend/
    ├── main.py          ← FastAPI proxy that talks to your Azure AI Foundry agent
    └── requirements.txt
```

The front end is a plain HTML/CSS/JS mockup of the page layout (placeholder
branding — swap the "AeroParts" logo/name for the real one if you have rights
to it). The floating icon in the bottom-right opens a chat panel that calls
`http://localhost:8000/api/chat`, which the backend forwards to your agent:

```
https://first-test-01-resource.services.ai.azure.com/api/projects/first-test-01/agents/supervisor-agent/endpoint/protocols/openai/responses
```

## 1. Sign in to Azure

This endpoint only accepts **Microsoft Entra ID bearer tokens** — API keys
are not supported on it. The backend uses `DefaultAzureCredential`, which
(in a local VS Code terminal) picks up your Azure CLI login. So before
starting the backend:

```bash
az login
```

Your account (or service principal, if you're running this non-interactively)
needs the **"Foundry User"** role — or a custom role with the
`Microsoft.CognitiveServices/accounts/AIServices/applications/invoke/action`
permission — on the `first-test-01-resource` Foundry resource. If you get a
403/authentication error later, this role assignment is the first thing to
check.

## 2. Install backend dependencies

```bash
cd aeroparts-mockup/backend
python -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure the agent (optional overrides)

`main.py` defaults to the resource/project/agent from your endpoint URL:

```
AZURE_FOUNDRY_RESOURCE = first-test-01-resource
AZURE_FOUNDRY_PROJECT  = first-test-01
AZURE_FOUNDRY_AGENT    = supervisor-agent
```

If you point this at a different agent later, override via env vars instead
of editing the file:

```bash
export AZURE_FOUNDRY_RESOURCE="my-other-resource"
export AZURE_FOUNDRY_PROJECT="my-other-project"
export AZURE_FOUNDRY_AGENT="my-other-agent"
```

## 4. Run the backend

```bash
uvicorn main:app --reload --port 8000
```

Check it's alive and pointed at the right endpoint:

```bash
curl http://localhost:8000/health
```

## 5. Open the front end

Just open `index.html` in a browser (double-click it, or `open index.html` /
`start index.html`). Click the floating chat icon bottom-right, type a
message, and it will call the backend, which calls your Foundry agent.

If the backend isn't running, or the Azure token/role isn't set up, the chat
will show a friendly error instead of hanging.

## Notes on how this agent endpoint behaves

- **Stateless**: the Responses API protocol on this agent endpoint does not
  keep conversation history server-side. The backend resends the full
  conversation (`history` + new message) on every call — this is already
  handled for you in `main.py` and in the widget's JS.
- **No streaming yet**: this version waits for the full reply and returns it
  in one shot. If you want token-by-token streaming in the chat panel, that's
  a follow-up — say so and I'll add it (SSE from FastAPI + incremental
  rendering in the widget).
- If the agent's JSON response shape doesn't match what `extract_output_text()`
  in `main.py` expects, that function logs the raw response and falls back to
  a generic message — check the `uvicorn` terminal output for the actual
  payload if replies come back empty.

## Swapping in the real Boeing branding

`index.html` uses a placeholder wordmark ("AeroParts") and a simple circular
mark instead of Boeing's actual logo, since this session can't reproduce a
real company's branding. If you have the rights to use it (e.g., this is an
internal/authorized build), just replace:

```html
<div class="brand-mark">AP</div>
<span class="brand-name">AeroParts</span>
```

with your actual logo `<img>` and name, and adjust `--blue`/`--accent` in the
`:root` CSS variables if the brand colors differ.