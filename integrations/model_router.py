import os
from config.settings import LLM_SMART_URL, LLM_FAST_URL

SMART_TRIGGERS = {
    "email", "schedule", "remind", "summarise", "summarize",
    "analyse", "analyze", "task", "search", "remember",
    "report", "document", "delegate", "draft", "meeting",
    "calendar", "agenda", "deadline", "priority", "contact",
    "send", "create", "add task", "update", "delete",
    "who is", "what is", "when is", "transcribe", "voice note",
    "list my", "show my", "digest", "brief", "summary"
}

def route_model(message: str) -> str:
    # Task 19: will return (url, mode) tuple
    if not message:
        return LLM_FAST_URL
    lowered = message.lower()
    for trigger in SMART_TRIGGERS:
        if trigger in lowered:
            return LLM_SMART_URL
    return LLM_FAST_URL
