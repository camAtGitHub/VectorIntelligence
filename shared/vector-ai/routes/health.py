"""GET /health."""
from fastapi import APIRouter

from debug_log import DEBUG
from llm import (
    LLM_AMBIENT_TEMPERATURE,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_GREETING_TEMPERATURE,
    LLM_JOKE_CRITIC_TEMPERATURE,
    LLM_JOKE_GENERATE_TEMPERATURE,
    LLM_MOOD_TEMPERATURE,
    LLM_SENSOR_TEMPERATURE,
    LLM_SUMMARY_TEMPERATURE,
    LLM_TEMPERATURE,
    MAX_HISTORY_MESSAGES,
    MODEL,
    SUMMARY_MODEL,
)

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
        "temperature": {
            "chat": LLM_TEMPERATURE,
            "summary": LLM_SUMMARY_TEMPERATURE,
            "mood": LLM_MOOD_TEMPERATURE,
            "ambient": LLM_AMBIENT_TEMPERATURE,
            "greeting": LLM_GREETING_TEMPERATURE,
            "sensor": LLM_SENSOR_TEMPERATURE,
            "joke_generate": LLM_JOKE_GENERATE_TEMPERATURE,
            "joke_critic": LLM_JOKE_CRITIC_TEMPERATURE,
        },
        "debug": DEBUG,
    }
