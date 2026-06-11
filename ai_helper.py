"""
ai_helper.py — Shared AI call utility for PBI Intelligence Platform.

Usage from any page:
    from ai_helper import ai_call, ai_available

    if ai_available():
        result = ai_call("You are a DAX expert.", "Explain: SUM(Sales[Amount])")
"""

import streamlit as st


def _secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default) or default
    except Exception:
        return default


def ai_available() -> bool:
    """Return True if any AI provider is configured via secrets."""
    return bool(
        _secret("GITHUB_TOKEN")
        or _secret("GROQ_API_KEY")
        or _secret("OPENAI_API_KEY")
        or _secret("ANTHROPIC_API_KEY")
    )


def ai_call(system_prompt: str, user_prompt: str, stream: bool = False) -> str:
    """
    Call the best available AI provider from secrets.
    Priority: GitHub Models (GPT-4o) → Groq → OpenAI → Anthropic
    Returns the full response as a string (stream=False default for page use).
    Raises RuntimeError if no provider is configured.
    """
    gh_token  = _secret("GITHUB_TOKEN")
    groq_key  = _secret("GROQ_API_KEY")
    oai_key   = _secret("OPENAI_API_KEY")
    ant_key   = _secret("ANTHROPIC_API_KEY")

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("`openai` package not installed. Run: pip install openai")

    if gh_token:
        client     = OpenAI(api_key=gh_token, base_url="https://models.inference.ai.azure.com")
        model_name = "gpt-4o"
    elif groq_key:
        client     = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
        model_name = "llama-3.3-70b-versatile"
    elif oai_key:
        client     = OpenAI(api_key=oai_key)
        model_name = "gpt-4o"
    elif ant_key:
        try:
            import anthropic as _ant
            _c = _ant.Anthropic(api_key=ant_key)
            msg = _c.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg.content[0].text
        except ImportError:
            raise RuntimeError("`anthropic` package not installed. Run: pip install anthropic")
    else:
        raise RuntimeError(
            "No AI provider configured. Add GITHUB_TOKEN, GROQ_API_KEY, or OPENAI_API_KEY "
            "to .streamlit/secrets.toml"
        )

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=1024,
    )
    return response.choices[0].message.content or ""
