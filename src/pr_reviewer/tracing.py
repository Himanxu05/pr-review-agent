"""Langfuse tracing, only if the LANGFUSE_* keys are set."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def tracing_callbacks() -> list:
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return []
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        log.warning("LANGFUSE keys set but langfuse is not installed: pip install '.[tracing]'")
        return []
    return [CallbackHandler()]
