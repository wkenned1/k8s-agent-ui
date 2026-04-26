# streamlit-app — testing UI for the K8s Agent

Multi-turn chat UI plus diagnostic tabs for `/verify` and
`/generate`. Talks to the deployed [`agent/`](../agent/) Worker (or a
local `wrangler dev` instance).

Three tabs:

- **💬 Chat** — multi-turn Q&A. Conversation history is held
  client-side and prepended to each new message (the Worker's
  `/chat` is stateless).
- **✓ Verify** — paste Lean source, see compile result + axioms +
  holes + trust verdict.
- **✦ Generate** — submit a natural-language intent, watch the
  two-stage discovery + draft pipeline run.

## Run locally

```sh
cd streamlit-app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Point at your Worker
export K8S_AGENT_WORKER_URL=http://localhost:8787

streamlit run app.py
```

Streamlit opens at <http://localhost:8501>. The Worker URL is also
editable in the sidebar at runtime — useful for switching between
local dev and a deployed instance.

## Deploy publicly

### Streamlit Community Cloud (free, simplest)

1. Push this repo to GitHub.
2. Sign in at <https://share.streamlit.io> with your GitHub account.
3. Click **New app**, select this repo + the `main` branch +
   `streamlit-app/app.py`.
4. Under **Advanced settings → Secrets**, add:

   ```toml
   K8S_AGENT_WORKER_URL = "https://k8s-agent.<your-subdomain>.workers.dev"

   # Required if you set AGENT_API_KEY on the Worker (recommended).
   # Same value as `wrangler secret put AGENT_API_KEY` produced.
   K8S_AGENT_API_KEY    = "<the-shared-secret>"

   # Optional: gate the UI itself behind a password. Anyone hitting
   # the SCC URL sees a password prompt before the form renders.
   STREAMLIT_UI_PASSWORD = "<a-strong-passphrase>"
   ```

5. Deploy. App URL looks like
   `https://<repo-name>-<random>.streamlit.app`.

The default `K8S_AGENT_WORKER_URL` points at `localhost:8787`, which
won't work in the cloud — set the secret before first run.

### Other Python hosts

The app is a single-file Streamlit script with two dependencies; any
Python host works:

- **Render**: Web Service, build command `pip install -r requirements.txt`,
  start command `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`.
- **Railway / Fly.io**: same shape; expose port 8501 by default.
- **Docker**: `python:3.12-slim` base, copy `streamlit-app/`,
  `pip install`, `CMD streamlit run app.py`.

Set `K8S_AGENT_WORKER_URL` as an environment variable on whichever
platform you pick.

## What the chat conversation prefix looks like

Because the Worker's `/chat` endpoint is stateless, the Streamlit
app prefixes each new question with a compressed summary of prior
turns:

```
Conversation so far:
Earlier I asked: <prior question>

You answered: <truncated answer>
Formally verified: <claim 1>; <claim 2>

Now: <new question>
```

This keeps continuity cheap without needing server-side state. If
the conversation history gets large, the answers are truncated to
~400 chars to keep the prefix bounded.

## Layout

```
streamlit-app/
├── app.py                ← single-file app
├── requirements.txt
├── .streamlit/config.toml
└── README.md             ← this file
```
