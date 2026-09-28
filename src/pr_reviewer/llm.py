"""Pick the chat model. Anything with tool calling works."""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from .config import Settings, get_settings

KEY_VARS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ConfigError(RuntimeError):
    pass


def get_chat_model(settings: Settings | None = None) -> BaseChatModel:
    s = settings or get_settings()
    provider = s.llm_provider.lower()
    if provider not in KEY_VARS:
        raise ConfigError(f"unknown LLM_PROVIDER '{s.llm_provider}', use groq/openai/anthropic")
    if not os.getenv(KEY_VARS[provider]):
        raise ConfigError(f"{KEY_VARS[provider]} is not set, add it to .env")

    kwargs = {"model": s.llm_model, "temperature": s.llm_temperature,
              "max_retries": s.max_retries}
    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(**kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(**kwargs)
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(**kwargs)
