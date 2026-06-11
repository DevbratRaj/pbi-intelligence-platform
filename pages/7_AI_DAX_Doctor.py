"""
7_AI_DAX_Doctor.py — AI DAX Doctor
PBI Intelligence Platform

Paste any DAX → AI explains it, fixes it, optimises it, or writes descriptions
for every undocumented measure in your model.
Supports OpenAI (GPT-4o) and Azure OpenAI.
"""

import sys
import io
import json
import re
import tempfile
import os
import zipfile
from pathlib import Path
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="AI DAX Doctor — PBI Intelligence Platform",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stSidebar"] { background-color: #0d1b2a; }
[data-testid="stSidebar"] * { color: #e0e8f0; }
[data-testid="stSidebar"] h1 {
    color:#fff; font-size:1.4rem; font-weight:700;
    padding-bottom:.5rem; border-bottom:1px solid #1e3a5f;
}
.dax-result-box {
    background:#f8faff; border:1px solid #c7d2fe; border-radius:10px;
    padding:18px 20px; margin-top:12px; line-height:1.7;
}
.fix-box {
    background:#f0fdf4; border:1px solid #86efac; border-radius:10px;
    padding:18px 20px; margin-top:12px;
}
.opt-box {
    background:#fffbeb; border:1px solid #fde68a; border-radius:10px;
    padding:18px 20px; margin-top:12px;
}
.perf-poor   { color:#dc2626; font-weight:700; }
.perf-fair   { color:#d97706; font-weight:700; }
.perf-good   { color:#16a34a; font-weight:700; }
.api-key-note { background:#eff6ff; border-radius:8px; padding:10px 14px;
    font-size:.84rem; color:#1e40af; margin-bottom:8px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Import project modules
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata

# ─────────────────────────────────────────────────────────────────────────────
# Auto-load credentials from Streamlit secrets (never committed to git)
# ─────────────────────────────────────────────────────────────────────────────
def _secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default) or default
    except Exception:
        return default

_SECRET_GITHUB    = _secret("GITHUB_TOKEN")
_SECRET_GROQ      = _secret("GROQ_API_KEY")
_SECRET_OPENAI    = _secret("OPENAI_API_KEY")
_SECRET_ANTHROPIC = _secret("ANTHROPIC_API_KEY")

# Pick the best default provider based on available secrets
if "dax_doc_provider" not in st.session_state:
    if _SECRET_ANTHROPIC:
        st.session_state["dax_doc_provider"] = "Anthropic (Claude)"
    elif _SECRET_GITHUB:
        st.session_state["dax_doc_provider"] = "GitHub Models (Claude / GPT-4o)"
    elif _SECRET_GROQ:
        st.session_state["dax_doc_provider"] = "Groq (Free)"
    elif _SECRET_OPENAI:
        st.session_state["dax_doc_provider"] = "OpenAI"
    else:
        st.session_state["dax_doc_provider"] = "⚡ Local (No API — instant)"

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — API key + model config
# ─────────────────────────────────────────────────────────────────────────────
_PROVIDERS = [
    "⚡ Local (No API — instant)",
    "GitHub Models (Claude / GPT-4o)",
    "Anthropic (Claude)",
    "Groq (Free)",
    "Ollama (Local — Free)",
    "OpenAI",
    "Azure OpenAI",
]

with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### 🔑 AI Configuration")

    _default_idx = _PROVIDERS.index(
        st.session_state.get("dax_doc_provider", _PROVIDERS[0])
        if st.session_state.get("dax_doc_provider") in _PROVIDERS
        else _PROVIDERS[0]
    )
    provider = st.radio(
        "Provider",
        _PROVIDERS,
        index=_default_idx,
        key="dax_doc_provider",
    )

    if provider == "⚡ Local (No API — instant)":
        st.markdown(
            "<div style='background:#f0fdf4;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
            "⚡ <strong>No API key needed.</strong> Descriptions are generated instantly on your PC "
            "by analysing the DAX expression — no data ever leaves your machine.</div>",
            unsafe_allow_html=True,
        )
        api_key = "local"
        azure_endpoint = None
        azure_deployment = None
        model = "local"

    elif provider == "Ollama (Local — Free)":
        st.markdown(
            "<div style='background:#f0fdf4;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
            "🖥️ <strong>Runs entirely on your PC</strong> — no account, no key, no data leaves your machine.<br/>"
            "<small>Need Ollama? <a href='https://ollama.com/download' target='_blank' style='color:#16a34a'>Download at ollama.com →</a></small></div>",
            unsafe_allow_html=True,
        )
        ollama_host = st.text_input(
            "Ollama host",
            value="http://localhost:11434",
            key="dax_doc_ollama_host",
        )
        model = st.selectbox(
            "Model (must be pulled locally)",
            ["llama3.2", "llama3.1", "codellama", "mistral", "phi3", "gemma2"],
            key="dax_doc_model",
        )
        api_key = "ollama"  # required by openai SDK but ignored by Ollama
        azure_endpoint = None
        azure_deployment = None

        # Live Ollama reachability check
        if st.button("🔗 Test Ollama connection", key="ollama_test"):
            import urllib.request
            try:
                with urllib.request.urlopen(
                    f"{ollama_host.rstrip('/')}/api/tags", timeout=3
                ) as r:
                    tags = json.loads(r.read())
                    model_list = [m["name"] for m in tags.get("models", [])]
                    if model_list:
                        st.success(f"✅ Ollama running — {len(model_list)} model(s): {', '.join(model_list[:5])}")
                    else:
                        st.warning("Ollama is running but no models are pulled yet.  \n"
                                   f"Run: `ollama pull {model}`")
            except Exception:
                st.error(f"❌ Cannot reach Ollama at `{ollama_host}`.  \n"
                         "Run `ollama serve` in a terminal first.")

    elif provider == "Groq (Free)":
        st.markdown(
            "<div style='background:#f0fdf4;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
            "✅ <strong>Free tier</strong> — fast, no credit card needed.<br/>"
            "<a href='https://console.groq.com/keys' target='_blank' style='color:#16a34a;font-size:.85rem'>"
            "Get free API key at console.groq.com →</a></div>",
            unsafe_allow_html=True,
        )
        if _SECRET_GROQ:
            st.caption("✅ Groq key loaded from secrets.")
            api_key = _SECRET_GROQ
        else:
            api_key = st.text_input(
                "Groq API Key",
                type="password",
                key="dax_doc_api_key",
                placeholder="gsk_...",
            )
        model = st.selectbox(
            "Model",
            ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
            key="dax_doc_model",
        )
        azure_endpoint = None
        azure_deployment = None

    elif provider == "GitHub Models (Claude / GPT-4o)":
        if _SECRET_GITHUB:
            st.markdown(
                "<div style='background:#f0fdf4;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
                "✅ <strong>GitHub token loaded from secrets.</strong> Ready to use — no input needed.</div>",
                unsafe_allow_html=True,
            )
            api_key = _SECRET_GITHUB
        else:
            st.markdown(
                "<div style='background:#eff6ff;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
                "ℹ️ Requires a GitHub PAT with <strong>Models: Read</strong> permission.<br/>"
                "<a href='https://github.com/settings/tokens/new' "
                "target='_blank' style='color:#1d4ed8;font-size:.85rem'>"
                "Create token →</a></div>",
                unsafe_allow_html=True,
            )
            api_key = st.text_input(
                "GitHub Token",
                type="password",
                key="dax_doc_api_key",
                placeholder="ghp_...",
            )
        model = st.selectbox(
            "Model",
            [
                "gpt-4o",
                "gpt-4o-mini",
                "Meta-Llama-3.1-70B-Instruct",
                "Mistral-large-2407",
                "Phi-4",
                "DeepSeek-R1",
                "xai-grok-3",
            ],
            index=0,
            key="dax_doc_model",
        )
        st.caption("💡 For Claude, switch to the **Anthropic (Claude)** provider above.")
        azure_endpoint = None
        azure_deployment = None

    elif provider == "Anthropic (Claude)":
        if _SECRET_ANTHROPIC:
            st.markdown(
                "<div style='background:#f0fdf4;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
                "✅ <strong>Anthropic key loaded from secrets.</strong> Ready to use.</div>",
                unsafe_allow_html=True,
            )
            api_key = _SECRET_ANTHROPIC
        else:
            st.markdown(
                "<div style='background:#fdf4ff;border-radius:8px;padding:10px 12px;margin-bottom:8px'>"
                "🟣 Get a key at <a href='https://console.anthropic.com/keys' "
                "target='_blank' style='color:#7c3aed;font-size:.85rem'>console.anthropic.com →</a></div>",
                unsafe_allow_html=True,
            )
            api_key = st.text_input(
                "Anthropic API Key",
                type="password",
                key="dax_doc_api_key",
                placeholder="sk-ant-...",
            )
        model = st.selectbox(
            "Model",
            [
                "claude-opus-4-5",
                "claude-sonnet-4-5",
                "claude-3-7-sonnet-20250219",
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022",
            ],
            index=1,
            key="dax_doc_model",
        )
        azure_endpoint = None
        azure_deployment = None

    elif provider == "OpenAI":
        if _SECRET_OPENAI:
            st.caption("✅ OpenAI key loaded from secrets.")
            api_key = _SECRET_OPENAI
        else:
            api_key = st.text_input(
                "OpenAI API Key",
                type="password",
                key="dax_doc_api_key",
                placeholder="sk-...",
            )
        model = st.selectbox(
            "Model",
            ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
            key="dax_doc_model",
        )
        azure_endpoint = None
        azure_deployment = None

    else:  # Azure OpenAI
        api_key = st.text_input(
            "Azure API Key",
            type="password",
            key="dax_doc_api_key",
            placeholder="Azure key",
        )
        azure_endpoint = st.text_input(
            "Azure Endpoint",
            key="dax_doc_azure_endpoint",
            placeholder="https://YOUR-RESOURCE.openai.azure.com/",
        )
        azure_deployment = st.text_input(
            "Deployment Name",
            key="dax_doc_azure_deployment",
            placeholder="gpt-4o",
        )
        model = azure_deployment or "gpt-4o"

    st.markdown("---")
    st.caption("Keys are kept in your browser session only — never saved to disk.")

# ─────────────────────────────────────────────────────────────────────────────
# Local DAX description engine (no API needed)
# ─────────────────────────────────────────────────────────────────────────────
def _local_describe(measure_name: str, expression: str, table: str = "") -> str:
    """
    Generate a plain-English description from a DAX expression using pattern
    matching — no API call required.
    """
    expr = (expression or "").strip()
    name = measure_name.strip()

    # ── Humanise the measure name ─────────────────────────────────────────────
    # Split CamelCase / PascalCase / snake_case / kebab-case into words
    human = re.sub(r"([A-Z])", r" \1", name).strip()
    human = re.sub(r"[_\-]+", " ", human)
    human = re.sub(r"\s+", " ", human).strip()

    if not expr:
        return f"Calculates {human}."

    expr_u = expr.upper()

    # ── Detect time intelligence ──────────────────────────────────────────────
    TI_FUNCS = {
        "DATESYTD": "year-to-date",
        "DATESMTD": "month-to-date",
        "DATESQTD": "quarter-to-date",
        "SAMEPERIODLASTYEAR": "same period last year",
        "DATEADD": "over a shifted time period",
        "PARALLELPERIOD": "for a parallel period",
        "PREVIOUSMONTH": "for the previous month",
        "PREVIOUSQUARTER": "for the previous quarter",
        "PREVIOUSYEAR": "for the previous year",
        "NEXTMONTH": "for the next month",
        "TOTALYTD": "year-to-date total",
        "TOTALMTD": "month-to-date total",
        "TOTALQTD": "quarter-to-date total",
    }
    ti_phrase = next((p for fn, p in TI_FUNCS.items() if fn in expr_u), None)

    # ── Detect aggregation ────────────────────────────────────────────────────
    AGG_MAP = {
        "SUMX":       "sum",   "SUM":        "total",
        "AVERAGEX":   "average", "AVERAGE":  "average",
        "COUNTROWS":  "count", "COUNT":      "count",
        "COUNTA":     "count", "COUNTX":     "count",
        "COUNTBLANK": "blank count",
        "MAXX":       "maximum", "MAX":      "maximum",
        "MINX":       "minimum", "MIN":      "minimum",
        "MEDIANX":    "median",  "MEDIAN":   "median",
        "DISTINCTCOUNT": "distinct count",
        "DIVIDE":     "ratio",
        "RANKX":      "rank",
        "PERCENTILEX.INC": "percentile",
    }
    agg = next((v for fn, v in AGG_MAP.items() if re.search(r'\b' + fn + r'\b', expr_u)), None)

    # ── Detect CALCULATE context modifier ────────────────────────────────────
    has_calculate  = "CALCULATE" in expr_u
    has_filter     = "FILTER" in expr_u
    has_all        = any(f in expr_u for f in ("ALL(", "ALLSELECTED(", "ALLEXCEPT(", "REMOVEFILTERS"))
    has_userel     = "USERELATIONSHIP" in expr_u
    has_divide     = "DIVIDE" in expr_u
    has_if         = re.search(r'\bIF\b', expr_u) is not None
    has_switch      = "SWITCH" in expr_u
    has_blank       = "BLANK()" in expr_u or "ISBLANK" in expr_u
    has_var        = "VAR " in expr_u

    # ── Extract referenced column/table for context ───────────────────────────
    col_match = re.search(r"[A-Za-z_][A-Za-z0-9_\s]*\[([A-Za-z0-9_\s]+)\]", expr)
    col_name  = col_match.group(1).strip() if col_match else ""

    # ── Build description ─────────────────────────────────────────────────────
    parts: list[str] = []

    if has_divide:
        nums = re.findall(r"\[([^\]]+)\]", expr)
        if len(nums) >= 2:
            parts.append(f"Calculates the ratio of [{nums[0]}] to [{nums[1]}]")
        else:
            parts.append(f"Calculates the {human} ratio")

    elif agg and col_name:
        parts.append(f"Calculates the {agg} of {col_name}")
    elif agg:
        parts.append(f"Calculates the {agg} of {human}")
    else:
        parts.append(f"Calculates {human}")

    if ti_phrase:
        parts[0] += f" ({ti_phrase})"

    if has_calculate:
        if has_filter and not has_all:
            parts.append("with specific filter conditions applied")
        elif has_all:
            parts.append("ignoring some or all active report filters")
        elif has_userel:
            parts.append("using an inactive relationship")

    if has_if or has_switch:
        parts.append("Returns different values depending on conditions")

    if has_blank:
        parts.append("Returns blank when the result is zero or undefined")

    base = ". ".join(parts).strip()
    if not base.endswith("."):
        base += "."

    # Append table context if available
    if table:
        base += f" Based on data from the {table} table."

    return base


# ─────────────────────────────────────────────────────────────────────────────
# AI call helper
# ─────────────────────────────────────────────────────────────────────────────
def _friendly_error(exc: Exception, prov: str) -> str:
    """Turn a raw openai/httpx exception into a clear, actionable message."""
    msg = str(exc)
    # Detect connection-level failures
    is_conn = any(k in type(exc).__name__ for k in ("Connection", "Connect", "Timeout", "Network"))
    is_conn = is_conn or "connection" in msg.lower() or "connect" in msg.lower()
    is_auth = any(k in type(exc).__name__ for k in ("Auth", "Unauthorized", "Forbidden"))
    is_auth = is_auth or "401" in msg or "403" in msg or "authentication" in msg.lower()
    is_rate = "429" in msg or "rate_limit" in msg.lower() or "RateLimit" in type(exc).__name__
    is_model = "model" in msg.lower() and ("not found" in msg.lower() or "404" in msg)

    if prov == "Ollama (Local — Free)":
        if is_conn:
            return (
                "**Ollama is not running.**\n\n"
                "Start it with:\n```\nollama serve\n```\n"
                "Then make sure the host in the sidebar matches (default: `http://localhost:11434`).  \n"
                "Don't have Ollama? [Download at ollama.com →](https://ollama.com/download)"
            )
        if is_model:
            mdl = st.session_state.get("dax_doc_model", "llama3.2")
            return (
                f"**Model `{mdl}` not found locally.**\n\n"
                f"Pull it first:\n```\nollama pull {mdl}\n```"
            )

    if prov in ("GitHub Models", "GitHub Models (Claude / GPT-4o)"):
        if is_conn:
            return (
                "**Cannot reach GitHub Models API** (`models.inference.ai.azure.com`).  \n\n"
                "Possible causes:\n"
                "- **Firewall / corporate proxy** blocking the endpoint — try on a personal network\n"
                "- **Token not entered** — paste your GitHub PAT in the sidebar\n"
                "- **Token type wrong** — needs a *classic* PAT or fine-grained PAT "
                "with **Models: Read** permission  \n"
                "[Create token →](https://github.com/settings/tokens/new)"
            )
        if is_auth:
            return (
                "**GitHub token rejected (401/403).**\n\n"
                "- Make sure the token has **Models: Read** permission\n"
                "- Verify the token hasn't expired\n"
                "[Create a new token →](https://github.com/settings/tokens/new)"
            )
        if is_model:
            return (
                "**Model not available on GitHub Models.**  \n"
                "Try `claude-3-5-sonnet`, `gpt-4o-mini`, or `Meta-Llama-3.1-70B-Instruct`."
            )

    if prov == "Anthropic (Claude)":
        if is_auth:
            return (
                "**Invalid Anthropic API key.**  \n"
                "Get a key at [console.anthropic.com/keys](https://console.anthropic.com/keys)."
            )
        if is_rate:
            return "**Anthropic rate limit hit.** Wait a few seconds and try again."
        if is_conn:
            return "**Cannot reach Anthropic API.** Check your internet connection."
        if is_model:
            return "**Model name not recognised.** Try `claude-sonnet-4-5` or `claude-3-5-sonnet-20241022`."

    if prov == "Groq (Free)":
        if is_auth:
            return (
                "**Invalid Groq API key.**  \n"
                "Get a free key at [console.groq.com/keys](https://console.groq.com/keys)."
            )
        if is_rate:
            return "**Groq rate limit hit.** Wait a few seconds and try again."

    if prov == "OpenAI":
        if is_auth:
            return "**Invalid OpenAI API key.** Check your key at [platform.openai.com](https://platform.openai.com/api-keys)."
        if is_rate:
            return "**OpenAI rate limit / quota exceeded.** Check your usage at platform.openai.com."
        if is_conn:
            return "**Cannot reach OpenAI API.** Check your internet connection or proxy settings."

    if prov == "Azure OpenAI":
        if is_conn:
            return (
                "**Cannot reach Azure OpenAI endpoint.**  \n"
                "Verify the endpoint URL is correct (e.g. `https://YOUR-RESOURCE.openai.azure.com/`)."
            )
        if is_auth:
            return "**Azure API key rejected.** Verify your key and deployment name in the sidebar."

    # Generic fallback — still more helpful than raw exception
    return (
        f"**AI call failed** (`{type(exc).__name__}`):  \n"
        f"```\n{msg[:400]}\n```\n\n"
        "Check: API key is entered · Provider is reachable · Model name is correct"
    )


def _call_ai(system_prompt: str, user_prompt: str, stream: bool = True):
    """
    Route AI calls to the selected provider.
    Returns a streaming generator (stream=True) or str (stream=False).
    """
    # Local mode — no API call at all
    if provider == "⚡ Local (No API — instant)":
        raise NotImplementedError("_call_ai should not be called in local mode")

    try:
        from openai import OpenAI, AzureOpenAI
    except ImportError:
        raise ImportError("The `openai` package is not installed. Run: pip install openai")

    if provider == "Anthropic (Claude)":
        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError("`anthropic` package not installed. Run: pip install anthropic")
        key = api_key or ""
        if not key:
            raise ValueError("No Anthropic API key. Enter your key in the sidebar.")
        _ant_client = _anthropic.Anthropic(api_key=key)
        if stream:
            # Return a generator of openai-compatible fake chunks
            def _ant_gen():
                with _ant_client.messages.stream(
                    model=model,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                ) as s:
                    for text_delta in s.text_stream:
                        yield type("_C", (), {
                            "choices": [type("_Ch", (), {
                                "delta": type("_D", (), {"content": text_delta})()
                            })()]
                        })()
            return _ant_gen()
        else:
            msg = _ant_client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg.content[0].text

    if provider == "Ollama (Local — Free)":
        host = st.session_state.get("dax_doc_ollama_host", "http://localhost:11434").rstrip("/")
        client = OpenAI(api_key="ollama", base_url=f"{host}/v1")
        model_name = model

    elif provider == "Groq (Free)":
        key = api_key or ""
        if not key:
            raise ValueError("No Groq API key provided. Enter your key in the sidebar.")
        client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
        model_name = model

    elif provider == "GitHub Models (Claude / GPT-4o)":
        key = api_key or ""
        if not key:
            raise ValueError("No GitHub token provided. Enter your token in the sidebar.")
        client = OpenAI(api_key=key, base_url="https://models.inference.ai.azure.com")
        model_name = model

    elif provider == "Azure OpenAI":
        key = api_key or ""
        endpoint = azure_endpoint or ""
        try:
            endpoint = endpoint or st.secrets.get("AZURE_OPENAI_ENDPOINT", "")
        except Exception:
            pass
        if not key or not endpoint:
            raise ValueError("Azure API key and endpoint are both required.")
        client = AzureOpenAI(api_key=key, azure_endpoint=endpoint, api_version="2024-02-01")
        model_name = azure_deployment or model

    else:  # OpenAI
        key = api_key or ""
        try:
            key = key or st.secrets.get("OPENAI_API_KEY", "")
        except Exception:
            pass
        if not key:
            raise ValueError("No OpenAI API key provided. Enter your key in the sidebar.")
        client = OpenAI(api_key=key)
        model_name = model

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,
        stream=stream,
    )

    if stream:
        return response
    else:
        return response.choices[0].message.content


def _stream_to_placeholder(placeholder, response_stream):
    """Stream response chunks into a Streamlit placeholder, return full text."""
    full = ""
    for chunk in response_stream:
        delta = chunk.choices[0].delta.content or ""
        full += delta
        placeholder.markdown(full + "▌")
    placeholder.markdown(full)
    return full


# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
st.title("🤖 AI DAX Doctor")
st.markdown(
    "Paste any DAX measure and let AI explain it in plain English, "
    "diagnose errors, suggest faster rewrites, or auto-document your entire model."
)

if not api_key:
    st.info(
        "🔑 **Easiest option → GitHub Models (Claude / GPT-4o):** Your GitHub token is already configured. "
        "Select it in the sidebar to use **Claude** or **GPT-4o** instantly — no extra signup needed."
    )

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tab_explain, tab_fix, tab_opt, tab_batch = st.tabs([
    "🔍 Explain",
    "🔧 Debug & Fix",
    "⚡ Optimise",
    "📝 Batch Describe",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — EXPLAIN
# ══════════════════════════════════════════════════════════════════════════════
with tab_explain:
    st.markdown("### 🔍 Plain-English Explainer")
    st.markdown(
        "Paste any DAX measure and get a clear, jargon-free explanation "
        "that any business user or stakeholder can understand."
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        explain_name = st.text_input(
            "Measure name (optional)",
            placeholder="e.g. YTD Revenue",
            key="ex_name",
        )
        explain_context = st.text_area(
            "Business context (optional)",
            placeholder="e.g. Finance report, monthly P&L dashboard",
            height=80,
            key="ex_ctx",
        )
    with col2:
        explain_dax = st.text_area(
            "DAX Expression",
            placeholder="CALCULATE(\n    SUM(Sales[Amount]),\n    DATESYTD('Date'[Date])\n)",
            height=180,
            key="ex_dax",
        )

    if st.button("🔍 Explain this measure", type="primary", key="btn_explain", use_container_width=True):
        if not explain_dax.strip():
            st.warning("Paste a DAX expression first.")
        elif provider == "⚡ Local (No API — instant)":
            desc = _local_describe(
                explain_name or "this measure",
                explain_dax.strip(),
                explain_context or "",
            )
            expr_u = explain_dax.upper()
            funcs_found = [fn for fn in [
                "CALCULATE", "SUMX", "FILTER", "ALL", "ALLSELECTED", "USERELATIONSHIP",
                "DIVIDE", "IF", "SWITCH", "DATEADD", "DATESYTD", "SAMEPERIODLASTYEAR",
                "RANKX", "TOPN", "VAR", "RETURN", "DISTINCTCOUNT",
            ] if fn in expr_u]
            cols_found = re.findall(r"\[([^\]]+)\]", explain_dax)
            st.markdown("#### 📋 Local Analysis Result")
            st.info(desc)
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**DAX functions detected:**")
                if funcs_found:
                    for fn in funcs_found:
                        st.markdown(f"- `{fn}`")
                else:
                    st.markdown("- *(none detected)*")
            with c2:
                st.markdown("**Columns / measures referenced:**")
                if cols_found:
                    for c in list(dict.fromkeys(cols_found))[:10]:
                        st.markdown(f"- `[{c}]`")
                else:
                    st.markdown("- *(none detected)*")
            st.caption(
                "⚡ This is a rule-based local analysis — no AI, no data leaves your machine.  \n"
                "For a full step-by-step AI explanation, select **Groq (Free)** in the sidebar "
                "and get a free key at [console.groq.com](https://console.groq.com/keys) (takes ~1 min)."
            )
        elif not api_key:
            st.error("Enter your API key in the sidebar.")
        else:
            system = (
                "You are a world-class Power BI and DAX expert who excels at translating "
                "technical DAX code into crystal-clear plain English for non-technical "
                "business stakeholders. Never use jargon without explaining it. "
                "Be concise but complete. Use bullet points where helpful."
            )
            name_part = f"Measure name: **{explain_name}**\n\n" if explain_name else ""
            ctx_part  = f"Business context: {explain_context}\n\n" if explain_context else ""
            user = (
                f"{name_part}{ctx_part}"
                f"DAX expression:\n```dax\n{explain_dax.strip()}\n```\n\n"
                "Please explain:\n"
                "1. **What this measure calculates** — in one sentence\n"
                "2. **How it works** — step by step, in plain English\n"
                "3. **When the result changes** — what filters or slicers affect it\n"
                "4. **A real-world example** — e.g. 'If a user selects January 2025, this returns...'\n"
                "5. **Who should use this** — which role/team would find this useful"
            )
            with st.spinner("AI is reading your DAX…"):
                try:
                    result_ph = st.empty()
                    stream = _call_ai(system, user, stream=True)
                    _stream_to_placeholder(result_ph, stream)
                except Exception as e:
                    st.error(_friendly_error(e, provider))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — DEBUG & FIX
# ══════════════════════════════════════════════════════════════════════════════
with tab_fix:
    st.markdown("### 🔧 Debug & Fix")
    st.markdown(
        "Paste broken or incorrect DAX — AI diagnoses what's wrong, "
        "explains the root cause, and rewrites a corrected version."
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        fix_name = st.text_input("Measure name (optional)", key="fix_name")
        fix_error = st.text_area(
            "Error message (optional)",
            placeholder="Paste the Power BI error message here if you have one",
            height=100,
            key="fix_error",
        )
        fix_intent = st.text_area(
            "What should this measure do?",
            placeholder="e.g. Sum of sales for the selected month, filtered to active customers",
            height=80,
            key="fix_intent",
        )
    with col2:
        fix_dax = st.text_area(
            "Broken DAX Expression",
            placeholder="Paste the broken DAX here…",
            height=220,
            key="fix_dax",
        )

    if st.button("🔧 Diagnose & Fix", type="primary", key="btn_fix", use_container_width=True):
        if not fix_dax.strip():
            st.warning("Paste a DAX expression first.")
        elif provider == "⚡ Local (No API — instant)":
            expr = fix_dax.strip()
            issues: list[str] = []
            tips:   list[str] = []

            if expr.count("(") != expr.count(")"):
                issues.append("🔴 **Unbalanced parentheses** — `(` and `)` count don't match. Check every function call is closed.")
            if expr.count('"') % 2 != 0:
                issues.append("🔴 **Unmatched quotes** — odd number of `\"` characters found.")
            if re.search(r'\bIF\b.*,\s*,', expr, re.IGNORECASE):
                issues.append("🔴 **Empty IF branch** — `IF(..., ,)` has a blank TRUE result. Provide a value or `BLANK()`.")
            if re.search(r'FILTER\s*\(\s*ALL\s*\(', expr, re.IGNORECASE):
                tips.append("💡 `FILTER(ALL(...))` pattern detected — this is a common performance anti-pattern. "
                            "Prefer `CALCULATETABLE(..., REMOVEFILTERS(...))` where possible.")
            if re.search(r'\bIF\b[^,]+,\s*\d+\s*,\s*\d+', expr, re.IGNORECASE):
                tips.append("💡 `IF` returning numeric constants — consider `DIVIDE()` or `SWITCH()` for cleaner logic.")
            if re.search(r'\bDIVIDE\s*\([^,]+,[^,)]+\)', expr, re.IGNORECASE):
                tips.append("💡 `DIVIDE()` missing 3rd argument — add a default value (e.g. `0` or `BLANK()`) to avoid unintended blanks.")
            if re.search(r'\bSUMX\s*\(\s*\w+\s*,\s*\[', expr, re.IGNORECASE):
                tips.append("💡 `SUMX` over a simple column — `SUM([Column])` is faster when no row-level calculation is needed.")

            st.markdown("#### 🔍 Local Syntax & Pattern Check")
            if issues:
                st.markdown("**Issues found:**")
                for iss in issues:
                    st.markdown(iss)
            else:
                st.success("✅ No obvious syntax errors detected locally.")
            if tips:
                st.markdown("**Suggestions:**")
                for t in tips:
                    st.markdown(t)
            st.caption(
                "⚡ Local mode checks syntax and known anti-patterns only.  \n"
                "For a full AI-powered diagnosis with a corrected rewrite, select **Groq (Free)** in the sidebar."
            )
        elif not api_key:
            st.error("Enter your API key in the sidebar.")
        else:
            system = (
                "You are a world-class Power BI DAX expert and debugger. "
                "Your job is to identify exactly what is wrong with a DAX expression, "
                "explain it clearly, and produce a corrected version. "
                "Format your response with clear sections: Diagnosis, Root Cause, Fixed DAX, Explanation."
            )
            parts = [f"DAX expression:\n```dax\n{fix_dax.strip()}\n```"]
            if fix_name:
                parts.insert(0, f"Measure name: {fix_name}")
            if fix_error:
                parts.append(f"\nError message from Power BI:\n{fix_error.strip()}")
            if fix_intent:
                parts.append(f"\nWhat it should do: {fix_intent.strip()}")
            parts.append(
                "\n\nPlease provide:\n"
                "## 🔴 Diagnosis\nWhat is wrong (one sentence).\n\n"
                "## 📖 Root Cause\nWhy this error occurs in DAX.\n\n"
                "## ✅ Fixed DAX\n```dax\n[corrected expression]\n```\n\n"
                "## 💡 What Changed\nBullet list of each change made and why."
            )
            with st.spinner("AI is diagnosing your DAX…"):
                try:
                    result_ph = st.empty()
                    stream = _call_ai(system, "\n".join(parts), stream=True)
                    _stream_to_placeholder(result_ph, stream)
                except Exception as e:
                    st.error(_friendly_error(e, provider))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — OPTIMISE
# ══════════════════════════════════════════════════════════════════════════════
with tab_opt:
    st.markdown("### ⚡ Performance Optimiser")
    st.markdown(
        "AI analyses your DAX for known anti-patterns — row-by-row iteration, "
        "nested FILTER, expensive CALCULATE contexts — and suggests a faster rewrite."
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        opt_name = st.text_input("Measure name (optional)", key="opt_name")
        opt_table_size = st.selectbox(
            "Approx. fact table size",
            ["< 100K rows", "100K – 1M rows", "1M – 10M rows", "> 10M rows", "Unknown"],
            index=4,
            key="opt_table_size",
        )
        opt_storage = st.selectbox(
            "Storage mode",
            ["Import", "DirectQuery", "Mixed / Unknown"],
            index=0,
            key="opt_storage",
        )
    with col2:
        opt_dax = st.text_area(
            "DAX Expression",
            placeholder="Paste the DAX you want to optimise…",
            height=220,
            key="opt_dax",
        )

    if st.button("⚡ Optimise", type="primary", key="btn_opt", use_container_width=True):
        if not opt_dax.strip():
            st.warning("Paste a DAX expression first.")
        elif provider == "⚡ Local (No API — instant)":
            expr = opt_dax.strip()
            expr_u = expr.upper()
            anti_patterns: list[tuple[str, str]] = []

            if re.search(r'\b(SUMX|AVERAGEX|MAXX|MINX)\s*\(.*\b(SUMX|AVERAGEX|MAXX|MINX)\b', expr_u, re.DOTALL):
                anti_patterns.append(("🔴 Nested iterator",
                    "Nested SUMX/AVERAGEX forces row-by-row evaluation at every level — very slow on large tables. "
                    "Refactor using CALCULATE + SUM where possible."))
            if re.search(r"FILTER\s*\(\s*(?!ALL\b|VALUES\b|CALCULATETABLE\b)'?[A-Za-z]", expr, re.IGNORECASE):
                anti_patterns.append(("🔴 FILTER on whole table",
                    "`FILTER(<Table>, ...)` scans every row. Use `CALCULATE(..., <condition>)` instead — "
                    "VertiPaq can apply it as a column filter without iterating rows."))
            if re.search(r'IFERROR\s*\(.*\b(CALCULATE|FILTER|SUMX)\b', expr_u, re.DOTALL):
                anti_patterns.append(("🟡 IFERROR wrapping heavy expression",
                    "`IFERROR` forces the engine to evaluate the expression twice on error. "
                    "Use `DIVIDE()` for division or check conditions with `IF`/`ISBLANK` instead."))
            if re.search(r'\bIF\b.*ISBLANK', expr_u):
                anti_patterns.append(("🟡 IF + ISBLANK pattern",
                    "Combine into `IF(ISBLANK(x), ...)` — already fine — but consider `COALESCE(x, default)` for simpler DAX."))
            if not re.search(r'\bVAR\b', expr_u) and expr_u.count('CALCULATE') > 2:
                anti_patterns.append(("🟡 Multiple CALCULATE calls without VAR",
                    "Repeated CALCULATE evaluations can be refactored using `VAR` to compute sub-expressions once."))

            score = max(0, 100 - len([a for a in anti_patterns if a[0].startswith("🔴")]) * 25
                                 - len([a for a in anti_patterns if a[0].startswith("🟡")]) * 10)
            label = "Good ✅" if score >= 80 else "Fair ⚠️" if score >= 55 else "Poor 🔴"

            st.markdown("#### ⚡ Local Performance Analysis")
            st.metric("Performance Rating", label, f"Score: {score}/100")

            if anti_patterns:
                st.markdown("**Anti-patterns detected:**")
                for title, detail in anti_patterns:
                    with st.expander(title):
                        st.markdown(detail)
            else:
                st.success("✅ No common anti-patterns detected. DAX looks clean!")
            st.caption(
                "⚡ Local mode uses static pattern matching — no AI, no data leaves your machine.  \n"
                "For an AI-generated optimised rewrite, select **Groq (Free)** in the sidebar."
            )
        elif not api_key:
            st.error("Enter your API key in the sidebar.")
        else:
            system = (
                "You are a Power BI DAX performance specialist with deep knowledge of "
                "the VertiPaq engine, DirectQuery query folding, and DAX evaluation contexts. "
                "Identify performance anti-patterns and produce an optimised version. "
                "Be specific about WHY each change improves performance."
            )
            user = (
                f"Measure name: {opt_name or 'Unknown'}\n"
                f"Storage mode: {opt_storage}\n"
                f"Fact table size: {opt_table_size}\n\n"
                f"DAX expression:\n```dax\n{opt_dax.strip()}\n```\n\n"
                "Please provide:\n"
                "## 🔍 Performance Rating\nRate the current DAX: **Poor / Fair / Good** and one-line reason.\n\n"
                "## ⚠ Anti-Patterns Found\nBullet list of each performance issue detected.\n\n"
                "## ⚡ Optimised DAX\n```dax\n[faster version]\n```\n\n"
                "## 📈 Why It's Faster\nExplain each change and its estimated impact.\n\n"
                "## 🧪 How to Verify\nHow to confirm the optimised version returns the same results."
            )
            with st.spinner("AI is analysing your DAX…"):
                try:
                    result_ph = st.empty()
                    stream = _call_ai(system, user, stream=True)
                    _stream_to_placeholder(result_ph, stream)
                except Exception as e:
                    st.error(_friendly_error(e, provider))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — BATCH DESCRIBE
# ══════════════════════════════════════════════════════════════════════════════
with tab_batch:
    st.markdown("### 📝 Batch Description Writer")
    st.markdown(
        "Upload your **.pbix / .pbit** file and AI will write plain-English descriptions "
        "for every measure that is missing one. Export the results to Excel and paste them "
        "back into Power BI Desktop."
    )

    # ── File source ──────────────────────────────────────────────────────────
    raw_bytes: bytes | None = st.session_state.get("pbi_file_bytes")

    if not raw_bytes:
        st.info("📂 No file loaded. Upload a .pbit / .pbix to get started.")
        up = st.file_uploader(
            "Upload Power BI file", type=["pbit", "pbix"],
            label_visibility="collapsed", key="batch_upload"
        )
        if up:
            raw_bytes = up.read()
            st.session_state["pbi_file_bytes"] = raw_bytes
            st.success(f"✅ {up.name} loaded")
            st.rerun()
        st.stop()

    # ── Parse metadata ───────────────────────────────────────────────────────
    @st.cache_data(show_spinner="Parsing model…")
    def _batch_meta(b: bytes) -> dict:
        return extract_pbit_metadata(io.BytesIO(b))

    try:
        meta = _batch_meta(raw_bytes)
    except Exception as exc:
        st.error(f"Could not parse file: {exc}")
        st.stop()

    measures = meta.get("measures", [])
    missing = [m for m in measures if not m.get("description")]
    has_desc = [m for m in measures if m.get("description")]

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Measures", len(measures))
    c2.metric("Missing Descriptions", len(missing))
    c3.metric("Already Documented", len(has_desc))

    if not missing:
        st.success("✅ All measures already have descriptions!")
        st.stop()

    st.markdown("---")

    # ── Filter controls ───────────────────────────────────────────────────────
    f1, f2 = st.columns(2)
    with f1:
        tables = sorted({m["table"] for m in missing})
        table_filter = st.multiselect("Filter by table", tables, key="batch_table_filter")
    with f2:
        max_measures = st.slider(
            "Max measures to describe (API cost control)",
            min_value=5, max_value=min(200, len(missing)),
            value=min(50, len(missing)),
            key="batch_max",
        )

    filtered_missing = missing
    if table_filter:
        filtered_missing = [m for m in missing if m["table"] in table_filter]
    filtered_missing = filtered_missing[:max_measures]

    is_local = (provider == "⚡ Local (No API — instant)")

    if is_local:
        st.info("⚡ **Local mode** — descriptions generated instantly from DAX pattern analysis. No API key required.")
        cost_note = "Free — no API calls"
    else:
        cost_note = f"~{len(filtered_missing) * 0.002:.2f} USD at GPT-4o-mini rates"

    st.caption(
        f"**{len(filtered_missing)}** measures selected. "
        f"Estimated cost: {cost_note}"
    )

    # Preview table
    with st.expander(f"📋 Preview {len(filtered_missing)} measures to describe", expanded=False):
        st.dataframe(
            pd.DataFrame([{
                "Table": m["table"],
                "Measure": m["name"],
                "DAX (preview)": (m.get("expression") or "")[:80],
            } for m in filtered_missing]),
            use_container_width=True, hide_index=True,
        )

    # ── Generate button ───────────────────────────────────────────────────────
    if not api_key:
        st.error("Enter your API key in the sidebar (or choose ⚡ Local).")
        st.stop()

    cache_key = f"batch_descs_{len(raw_bytes)}_{len(filtered_missing)}"

    if not is_local:
        batch_size = st.select_slider(
            "Measures per AI call (higher = faster, uses more tokens per request)",
            options=[1, 3, 5, 10, 15, 20],
            value=10,
            key="batch_size_slider",
            help="Batching 10 measures per call is ~10x faster than 1 at a time.",
        )
    else:
        batch_size = len(filtered_missing)  # process all at once locally

    if st.button(
        f"📝 Generate {len(filtered_missing)} Descriptions",
        type="primary",
        key="btn_batch",
        use_container_width=True,
    ):
        # ── LOCAL fast path — no API ──────────────────────────────────────────
        if is_local:
            results = []
            prog = st.progress(0.0, text="Analysing DAX expressions…")
            for i, m in enumerate(filtered_missing):
                expr = m.get("expression", "") or ""
                if isinstance(expr, list):
                    expr = "\n".join(expr)
                results.append({
                    "Table":       m["table"],
                    "Measure":     m["name"],
                    "Description": _local_describe(m["name"], expr, m["table"]),
                    "DAX":         expr[:120],
                })
                prog.progress((i + 1) / len(filtered_missing))
            prog.empty()
            st.session_state[cache_key] = results
            st.success(f"✅ Generated {len(results)} descriptions instantly (local mode)!")

        else:
            # ── API batch path ────────────────────────────────────────────────
            system = (
                "You are a Power BI documentation expert. "
                "You will receive a numbered list of DAX measures. "
                "Return ONLY a valid JSON array — no markdown, no explanation — where each element has "
                '{"index": <number>, "name": "<measure name>", "description": "<1-2 sentence plain-English description>"}. '
                "Keep descriptions concise and jargon-free."
            )

            results      = []
            error_count  = 0
            MAX_ERRORS   = 3

            chunks = [
                filtered_missing[i: i + batch_size]
                for i in range(0, len(filtered_missing), batch_size)
            ]

            progress  = st.progress(0.0, text="Starting…")
            status    = st.empty()
            cancelled = False

            for chunk_idx, chunk in enumerate(chunks):
                if cancelled:
                    break

                lines = []
                for j, m in enumerate(chunk):
                    expr = m.get("expression", "") or ""
                    if isinstance(expr, list):
                        expr = "\n".join(expr)
                    lines.append(
                        f"{j+1}. Name: {m['name']} | Table: {m['table']} | DAX: {expr[:300]}"
                    )
                user_prompt = "Describe these DAX measures:\n\n" + "\n".join(lines)

                try:
                    raw_resp = _call_ai(system, user_prompt, stream=False)
                    error_count = 0

                    parsed: list[dict] = []
                    try:
                        clean = re.sub(r"```(?:json)?|```", "", raw_resp).strip()
                        parsed = json.loads(clean)
                    except Exception:
                        m_json = re.search(r"\[.*\]", raw_resp, re.DOTALL)
                        if m_json:
                            try:
                                parsed = json.loads(m_json.group())
                            except Exception:
                                parsed = []

                    desc_by_name = {
                        str(p.get("name", "")).strip().lower(): p.get("description", "").strip()
                        for p in (parsed if isinstance(parsed, list) else [])
                    }

                    for m in chunk:
                        desc = desc_by_name.get(m["name"].lower(), "")
                        if not desc:
                            for p in (parsed if isinstance(parsed, list) else []):
                                if str(p.get("index", "")) == str(chunk.index(m) + 1):
                                    desc = p.get("description", "").strip()
                                    break
                        expr = m.get("expression", "") or ""
                        results.append({
                            "Table":       m["table"],
                            "Measure":     m["name"],
                            "Description": desc or "[Not returned by AI]",
                            "DAX":         (expr if isinstance(expr, str) else "\n".join(expr))[:120],
                        })

                except Exception as e:
                    error_count += 1
                    err_msg = _friendly_error(e, provider)
                    if error_count >= MAX_ERRORS:
                        progress.empty()
                        status.empty()
                        st.error(f"**Stopped after {MAX_ERRORS} consecutive failures.**\n\n{err_msg}")
                        cancelled = True
                        break
                    else:
                        status.warning(f"⚠️ Chunk {chunk_idx+1} failed ({error_count}/{MAX_ERRORS}): {str(e)[:80]}")
                        for m in chunk:
                            expr = m.get("expression", "") or ""
                            results.append({
                                "Table":       m["table"],
                                "Measure":     m["name"],
                                "Description": "[Connection error — check provider/key in sidebar]",
                                "DAX":         (expr if isinstance(expr, str) else "\n".join(expr))[:120],
                            })

                done = min((chunk_idx + 1) * batch_size, len(filtered_missing))
                progress.progress(done / len(filtered_missing), text=f"{done} / {len(filtered_missing)} measures")

            progress.empty()
            status.empty()

            if results:
                st.session_state[cache_key] = results
                good = sum(1 for r in results if not r["Description"].startswith("["))
                if good == len(results):
                    st.success(f"✅ Generated {len(results)} descriptions!")
                else:
                    st.warning(f"Generated {good} descriptions. {len(results) - good} failed — check your API key/provider.")

    # ── Show results ──────────────────────────────────────────────────────────
    if cache_key in st.session_state:
        results = st.session_state[cache_key]
        df_results = pd.DataFrame(results)
        st.dataframe(
            df_results[["Table", "Measure", "Description"]],
            use_container_width=True,
            hide_index=True,
        )

        # ── Excel export ──────────────────────────────────────────────────────
        _buf = io.BytesIO()
        with pd.ExcelWriter(_buf, engine="openpyxl") as _writer:
            df_results.to_excel(_writer, index=False, sheet_name="AI Descriptions")
            _ws = _writer.sheets["AI Descriptions"]
            from openpyxl.styles import Font as _Font, PatternFill as _Fill, Alignment as _Align
            for _cell in _ws[1]:
                _cell.font  = _Font(bold=True, color="FFFFFF")
                _cell.fill  = _Fill(fill_type="solid", fgColor="1E3A5F")
                _cell.alignment = _Align(horizontal="center")
            for _col, _w in zip("ABCD", [22, 35, 70, 60]):
                _ws.column_dimensions[_col].width = _w
            for _row in _ws.iter_rows(min_row=2):
                for _cell in _row:
                    _cell.alignment = _Align(wrap_text=True, vertical="top")

        st.download_button(
            label="⬇ Export Descriptions to Excel",
            data=_buf.getvalue(),
            file_name="ai_measure_descriptions.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            type="primary",
        )
        st.caption(
            "**How to apply:** Open the Excel file → copy descriptions → "
            "paste into Power BI Desktop: Model view → select measure → Properties pane → Description field."
        )
