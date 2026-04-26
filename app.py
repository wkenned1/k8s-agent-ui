"""K8s Agent — Streamlit testing UI.

Multi-turn chat interface against the deployed Worker.
Configurable Worker URL, precise/quick mode toggle, expanders for
verified facts / provisional facts / citations. Two side tabs let
power users hit `/verify` and `/generate` directly.

Conversation state is held in `st.session_state` and re-sent as a
prefix on each turn — the Worker's `/chat` endpoint is stateless,
so multi-turn context is the client's responsibility.
"""

from __future__ import annotations

import json
import os
import time

import requests
import streamlit as st

# ─── Config ───────────────────────────────────────────────────────

DEFAULT_WORKER_URL = os.getenv(
    "K8S_AGENT_WORKER_URL",
    "http://localhost:8787",
)
TIMEOUT_SECONDS = 120


def _secret(name: str, default: str = "") -> str:
    """Read from st.secrets first (deployed), then env (local)."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except (FileNotFoundError, KeyError):
        pass
    return os.getenv(name, default)


# Optional shared API key for the Worker. When set on the Worker via
# `wrangler secret put AGENT_API_KEY`, callers must send the same
# value as `Authorization: Bearer …`. Stored in SCC's Secrets, never
# in the public mirror.
AGENT_API_KEY = _secret("K8S_AGENT_API_KEY")

# Optional UI password gating. Renders a password prompt before the
# tabs; once entered correctly, the session unlocks. Defense in depth
# — without it, anyone who finds the SCC URL sees the form (still
# can't call the Worker without AGENT_API_KEY, but no need to expose
# the form at all).
UI_PASSWORD = _secret("STREAMLIT_UI_PASSWORD")

st.set_page_config(
    page_title="K8s Agent",
    page_icon="⎈",
    layout="wide",
)

# ─── Session state ────────────────────────────────────────────────

if "messages" not in st.session_state:
    # Each message: {"role": "user", "content": str}
    #            or {"role": "assistant", "kind": "answer"|"clarification"|"error", ...}
    st.session_state.messages = []
if "worker_url" not in st.session_state:
    st.session_state.worker_url = DEFAULT_WORKER_URL


# ─── Helpers ──────────────────────────────────────────────────────

def _auth_headers() -> dict:
    """Authorization header for cost-bearing endpoints. Empty when
    AGENT_API_KEY is unset (e.g. local dev against a Worker without
    the secret)."""
    return {"Authorization": f"Bearer {AGENT_API_KEY}"} if AGENT_API_KEY else {}


def call_chat(message: str, mode: str) -> dict:
    """POST /chat. Returns the parsed JSON body augmented with the
    HTTP status code so the UI can surface both halves of an error
    response."""
    url = f"{st.session_state.worker_url.rstrip('/')}/chat"
    try:
        response = requests.post(
            url,
            json={"message": message, "mode": mode},
            headers=_auth_headers(),
            timeout=TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        return {
            "kind": "error",
            "error": f"Request failed: {e}",
            "_status": None,
            "_url": url,
        }
    try:
        body = response.json()
    except json.JSONDecodeError:
        return {
            "kind": "error",
            "error": f"Non-JSON response (HTTP {response.status_code})",
            "detail": response.text[:1000],
            "_status": response.status_code,
            "_url": url,
        }
    body["_status"] = response.status_code
    body["_url"] = url
    return body


def call_health() -> dict:
    """GET /health for the configured Worker. Used by the sidebar
    health-check button to confirm the URL is reachable and which
    runner mode (http / container / unconfigured) is active.

    /health is intentionally unauthenticated so monitoring still works
    when the API key is wrong — but we attach the header anyway in
    case future versions of the Worker change that policy."""
    url = f"{st.session_state.worker_url.rstrip('/')}/health"
    try:
        response = requests.get(url, headers=_auth_headers(), timeout=10)
        return {
            "_status": response.status_code,
            "_url": url,
            **(response.json() if response.headers.get("content-type", "").startswith("application/json") else {"text": response.text[:500]}),
        }
    except requests.exceptions.RequestException as e:
        return {"_status": None, "_url": url, "error": str(e)}


def call_verify(code: str, theorem_name: str | None) -> dict:
    body: dict = {"code": code}
    if theorem_name:
        body["theoremName"] = theorem_name
    try:
        response = requests.post(
            f"{st.session_state.worker_url.rstrip('/')}/verify",
            json=body,
            headers=_auth_headers(),
            timeout=TIMEOUT_SECONDS,
        )
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {e}"}


def call_generate(intent: str, max_attempts: int) -> dict:
    try:
        response = requests.post(
            f"{st.session_state.worker_url.rstrip('/')}/generate",
            json={"intent": intent, "maxAttempts": max_attempts},
            headers=_auth_headers(),
            timeout=TIMEOUT_SECONDS,
        )
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {e}"}


MAX_PREFIX_CHARS = 4000  # bge-base-en-v1.5 caps embeddings around 512 tokens


def build_conversation_prefix(messages: list[dict]) -> str:
    """Compress prior Q+A pairs into a 'Conversation so far' prefix.

    Stateless `/chat` doesn't track history, so we manually prepend
    the prior turns so the agent has continuity for follow-ups.
    Truncates assistant answers to keep the prefix manageable, then
    drops the oldest turns if the assembled prefix exceeds
    MAX_PREFIX_CHARS (the AI Search retrieval embedding has a hard
    token cap and a too-long prompt yields opaque 502s).
    """
    if not messages:
        return ""
    parts: list[str] = []
    for msg in messages:
        if msg["role"] == "user":
            parts.append(f"Earlier I asked: {msg['content']}")
        elif msg.get("kind") == "answer":
            answer_excerpt = msg.get("answer", "")[:400]
            verified = msg.get("verifiedClaims") or []
            line = f"You answered: {answer_excerpt}"
            if verified:
                line += "\nFormally verified: " + "; ".join(verified[:3])
            parts.append(line)
        elif msg.get("kind") == "clarification":
            qs = msg.get("questions") or []
            qtexts = "; ".join(q.get("question", "") for q in qs[:3])
            parts.append(
                f"You asked for clarification: {qtexts}",
            )
    while parts and sum(len(p) for p in parts) > MAX_PREFIX_CHARS:
        parts.pop(0)
    if not parts:
        return ""
    return "Conversation so far:\n" + "\n\n".join(parts) + "\n\nNow: "


def render_lean_indicator(msg: dict) -> None:
    """Always-visible badge showing whether Lean verification fired
    on this turn. Critical signal — without it the user can't tell
    if the answer is RAG-only or Lean-grounded.

    Four states:
      - ✓ green : at least one trusted claim verified for the user's case
      - 💡 blue : only conditional/potentially-relevant facts (general
        theorem holds, but the question didn't pin down whether the
        precondition applies)
      - ~ blue  : only provisional (runtime-generated) claims fired
      - ○ gray  : no Lean claims at all; answer is RAG-only
    """
    verified = msg.get("verifiedClaims") or []
    conditional = msg.get("potentiallyRelevantClaims") or []
    tentative = msg.get("tentativeClaims") or []
    if verified:
        extra = f" (+{len(conditional)} conditional)" if conditional else ""
        st.success(
            f"✓ Lean: {len(verified)} fact"
            + ("s" if len(verified) != 1 else "")
            + " formally verified"
            + extra
        )
    elif conditional:
        st.info(
            f"💡 Lean: {len(conditional)} conditionally-relevant fact"
            + ("s" if len(conditional) != 1 else "")
            + " (general theorem holds; answer scaffolds your specific case)"
        )
    elif tentative:
        st.info(
            f"~ Lean: {len(tentative)} provisional fact"
            + ("s" if len(tentative) != 1 else "")
            + " (runtime-generated, unreviewed)"
        )
    else:
        st.caption("○ Lean: no claims fired for this question (answer is RAG-only)")


def render_error(msg: dict) -> None:
    """Render the full error envelope from `/chat`.

    The Worker returns `{ error, detail?, hint?, ... }` on retrieval
    failures with HTTP 502; surfacing only `error` hides the actual
    cause. We also show the URL/status as a caption so it's obvious
    which Worker the UI is hitting (sidebar URL is editable).
    """
    st.error(msg.get("error", "Unknown error"))
    detail = msg.get("detail")
    if detail:
        with st.expander("Detail"):
            st.code(detail, language=None)
    hint = msg.get("hint")
    if hint:
        st.info(f"Hint: {hint}")
    status = msg.get("_status")
    url = msg.get("_url")
    if status is not None or url:
        bits = []
        if status is not None:
            bits.append(f"HTTP {status}")
        if url:
            bits.append(url)
        st.caption(" · ".join(bits))


# ─── Sidebar ──────────────────────────────────────────────────────

with st.sidebar:
    st.title("⎈ K8s Agent")
    st.caption("Proof-guided Kubernetes assistant")

    st.session_state.worker_url = st.text_input(
        "Worker URL",
        value=st.session_state.worker_url,
        help="The deployed agent's HTTPS URL, or http://localhost:8787 for local dev.",
    )

    mode = st.radio(
        "Mode",
        options=["precise", "quick"],
        index=0,
        help=(
            "**precise**: ask clarifying questions when the formal model "
            "needs more facts. **quick**: answer immediately with whatever "
            "can be verified."
        ),
    )

    st.divider()

    if st.button("🗑  Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    if st.button("⚕  Check Worker health", use_container_width=True):
        with st.spinner("Pinging /health…"):
            health = call_health()
        status = health.get("_status")
        if status == 200:
            st.success(f"Reachable · HTTP {status}")
        elif status is None:
            st.error(health.get("error", "Connection failed"))
        else:
            st.warning(f"HTTP {status}")
        runner = health.get("leanRunner") or {}
        if runner:
            st.caption(f"Lean runner: `{runner.get('mode', '?')}`")
        ai_search = health.get("aiSearchInstance")
        if ai_search:
            st.caption(f"AI Search: `{ai_search}`")
        with st.expander("Raw /health response"):
            st.json(health)

    st.divider()

    st.caption("**Tabs above** let you also hit `/verify` and `/generate` directly.")


# ─── Password gate ────────────────────────────────────────────────
#
# Only renders when STREAMLIT_UI_PASSWORD is set in secrets. Defense
# in depth — without it, anyone with the SCC URL sees the form (they
# still can't call the Worker without K8S_AGENT_API_KEY, but we'd
# rather not even surface the form to randoms).

if UI_PASSWORD:
    if "ui_unlocked" not in st.session_state:
        st.session_state.ui_unlocked = False

    if not st.session_state.ui_unlocked:
        st.title("⎈ K8s Agent")
        st.caption("Enter the access password to continue.")
        with st.form("ui_password_form", clear_on_submit=True):
            attempt = st.text_input(
                "Password",
                type="password",
                label_visibility="collapsed",
                placeholder="Password",
            )
            submitted = st.form_submit_button("Unlock", use_container_width=True)
        if submitted:
            if attempt == UI_PASSWORD:
                st.session_state.ui_unlocked = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()


# ─── Tabs ─────────────────────────────────────────────────────────

tab_chat, tab_verify, tab_generate = st.tabs(
    ["💬 Chat", "✓ Verify", "✦ Generate"]
)


# ─── Chat tab ─────────────────────────────────────────────────────

with tab_chat:
    # History
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.write(msg["content"])
                continue

            kind = msg.get("kind")
            if kind == "answer":
                render_lean_indicator(msg)
                warning = msg.get("retrievalWarning")
                if warning:
                    st.warning(warning)
                st.markdown(msg.get("answer", "_(no answer text)_"))

                verified = msg.get("verifiedClaims") or []
                if verified:
                    with st.expander(
                        f"✓ {len(verified)} formally verified fact"
                        + ("s" if len(verified) != 1 else "")
                    ):
                        for c in verified:
                            st.markdown(f"- {c}")

                conditional = msg.get("potentiallyRelevantClaims") or []
                if conditional:
                    with st.expander(
                        f"💡 {len(conditional)} conditionally-relevant fact"
                        + ("s" if len(conditional) != 1 else "")
                        + " (general theorem holds; would apply if precondition is true)"
                    ):
                        for c in conditional:
                            st.markdown(f"- _{c}_")

                tentative = msg.get("tentativeClaims") or []
                if tentative:
                    with st.expander(
                        f"~ {len(tentative)} provisional fact"
                        + ("s" if len(tentative) != 1 else "")
                        + " (runtime-generated, not yet promoted)"
                    ):
                        for c in tentative:
                            st.markdown(f"- _{c}_")

                clarifying = msg.get("clarifyingQuestions") or []
                if clarifying:
                    with st.expander(
                        f"❓ {len(clarifying)} follow-up question"
                        + ("s" if len(clarifying) != 1 else "")
                        + " that would unlock direct verification"
                    ):
                        for q in clarifying:
                            st.markdown(
                                f"**{q.get('question', '?')}**\n\n"
                                f"_{q.get('reason', '')}_"
                            )

                citations = msg.get("citations") or []
                if citations:
                    with st.expander(
                        f"📎 {len(citations)} source"
                        + ("s" if len(citations) != 1 else "")
                    ):
                        for src in citations:
                            st.code(src, language=None)
            elif kind == "clarification":
                st.warning(msg.get("note", ""))
                for q in msg.get("questions", []):
                    st.markdown(
                        f"**{q.get('question', '?')}**\n\n"
                        f"_{q.get('reason', '')}_"
                    )
                unlockable = msg.get("unlockableClaims") or []
                if unlockable:
                    with st.expander(
                        f"What this would unlock if you answer ({len(unlockable)})"
                    ):
                        for c in unlockable:
                            st.markdown(f"- {c}")
            elif kind == "error":
                render_error(msg)

    # Input
    prompt = st.chat_input("Ask a Kubernetes question…")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)

        prefix = build_conversation_prefix(st.session_state.messages[:-1])
        full = prefix + prompt if prefix else prompt

        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                start = time.time()
                body = call_chat(full, mode)
                elapsed_ms = int((time.time() - start) * 1000)

            # Normalize the response to the shape we render.
            if "error" in body and "kind" not in body:
                body["kind"] = "error"

            st.session_state.messages.append({"role": "assistant", **body})

            if body.get("kind") == "answer":
                render_lean_indicator(body)
                warning = body.get("retrievalWarning")
                if warning:
                    st.warning(warning)
                st.markdown(body.get("answer", "_(no answer)_"))
                verified = body.get("verifiedClaims") or []
                if verified:
                    with st.expander(
                        f"✓ {len(verified)} formally verified fact"
                        + ("s" if len(verified) != 1 else "")
                    ):
                        for c in verified:
                            st.markdown(f"- {c}")
                conditional = body.get("potentiallyRelevantClaims") or []
                if conditional:
                    with st.expander(
                        f"💡 {len(conditional)} conditionally-relevant fact"
                        + ("s" if len(conditional) != 1 else "")
                        + " (general theorem holds; would apply if precondition is true)"
                    ):
                        for c in conditional:
                            st.markdown(f"- _{c}_")
                tentative = body.get("tentativeClaims") or []
                if tentative:
                    with st.expander(
                        f"~ {len(tentative)} provisional fact"
                        + ("s" if len(tentative) != 1 else "")
                    ):
                        for c in tentative:
                            st.markdown(f"- _{c}_")
                clarifying = body.get("clarifyingQuestions") or []
                if clarifying:
                    with st.expander(
                        f"❓ {len(clarifying)} follow-up question"
                        + ("s" if len(clarifying) != 1 else "")
                        + " that would unlock direct verification"
                    ):
                        for q in clarifying:
                            st.markdown(
                                f"**{q.get('question', '?')}**\n\n"
                                f"_{q.get('reason', '')}_"
                            )
                citations = body.get("citations") or []
                if citations:
                    with st.expander(
                        f"📎 {len(citations)} source"
                        + ("s" if len(citations) != 1 else "")
                    ):
                        for src in citations:
                            st.code(src, language=None)
            elif body.get("kind") == "clarification":
                st.warning(body.get("note", ""))
                for q in body.get("questions", []):
                    st.markdown(
                        f"**{q.get('question', '?')}**\n\n"
                        f"_{q.get('reason', '')}_"
                    )
            else:
                render_error(body)

            st.caption(f"⏱ {elapsed_ms} ms")


# ─── Verify tab ───────────────────────────────────────────────────

with tab_verify:
    st.markdown(
        "Send a Lean fragment directly to the runner. Useful for testing "
        "claim probes and inspecting axiom dependencies."
    )

    code = st.text_area(
        "Lean source",
        value="""import K8sLib.Universal
open K8sLib.Core K8sLib.Universal

theorem demo (s : Service) (h : s.type = .ClusterIP) :
    ¬ Service.isExternallyAccessible s :=
  Service.clusterip_not_externally_accessible h
""",
        height=240,
    )
    theorem_name = st.text_input(
        "Theorem name (optional, for `#print axioms`)",
        value="demo",
    )

    if st.button("Verify", key="verify_run"):
        with st.spinner("Compiling…"):
            result = call_verify(code, theorem_name or None)
        if "error" in result:
            st.error(result["error"])
        else:
            col1, col2, col3 = st.columns(3)
            col1.metric("Compiled", "✓" if result.get("compiled") else "✗")
            col2.metric("Trusted", "✓" if result.get("trusted") else "✗")
            col3.metric("Duration", f"{result.get('durationMs', 0)} ms")

            errs = result.get("errors") or []
            if errs:
                with st.expander(f"Errors ({len(errs)})"):
                    for e in errs:
                        st.code(e, language=None)
            warns = result.get("warnings") or []
            if warns:
                with st.expander(f"Warnings ({len(warns)})"):
                    for w in warns:
                        st.code(w, language=None)
            axioms = result.get("axioms") or []
            if axioms:
                with st.expander(f"Axiom dependencies ({len(axioms)})"):
                    for a in axioms:
                        st.code(a, language=None)
            holes = result.get("holes") or []
            if holes:
                with st.expander(f"Unsolved holes ({len(holes)})"):
                    for h in holes:
                        st.markdown(
                            f"- **{h.get('name', '?')}**: `{h.get('goal', '?')}`"
                        )


# ─── Generate tab ─────────────────────────────────────────────────

with tab_generate:
    st.markdown(
        "Ask the agent to generate a Lean theorem from natural language. "
        "Stage A tries automatic discovery (`exact?` / `apply?` / `aesop`); "
        "Stage B falls back to model-written proofs with a repair loop."
    )

    intent = st.text_area(
        "Intent",
        value="A Service of type ClusterIP cannot be reached from outside the cluster.",
        height=120,
    )
    max_attempts = st.slider("Max attempts", 1, 5, 3)

    if st.button("Generate", key="generate_run"):
        with st.spinner("Drafting and verifying…"):
            result = call_generate(intent, max_attempts)
        if "error" in result:
            st.error(result["error"])
        else:
            col1, col2, col3 = st.columns(3)
            col1.metric(
                "Outcome",
                "✓ Succeeded" if result.get("succeeded") else "✗ Failed",
            )
            col2.metric("Attempts", str(result.get("attempts", 0)))
            col3.metric(
                "Trust",
                result.get("classification", "?"),
            )

            theorem = result.get("theorem")
            if theorem:
                st.markdown(f"**Statement.** {theorem.get('statement', '')}")
                with st.expander("Generated Lean source"):
                    st.code(theorem.get("code", ""), language="lean")

            errors = result.get("errors") or []
            if errors:
                with st.expander(f"Compile errors on final attempt ({len(errors)})"):
                    for e in errors:
                        st.code(e, language=None)

            verify_result = result.get("verifyResult")
            if verify_result:
                axioms = verify_result.get("axioms") or []
                if axioms:
                    with st.expander(f"Axiom dependencies ({len(axioms)})"):
                        for a in axioms:
                            st.code(a, language=None)
