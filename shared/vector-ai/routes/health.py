"""GET /health."""
from fastapi import APIRouter

from debug_log import DEBUG
from llm import LLM_API_KEY, LLM_BASE_URL, MAX_HISTORY_MESSAGES, MODEL, SUMMARY_MODEL

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL,
        "summary_model": SUMMARY_MODEL,
        "llm_base": LLM_BASE_URL,
        "api_key_set": bool(LLM_API_KEY),
        "max_history_messages": MAX_HISTORY_MESSAGES,
        "debug": DEBUG,
    }
