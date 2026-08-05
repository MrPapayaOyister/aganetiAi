from backend.services import llm as _llm


def call_llm_summarize(text: str) -> str:
    """Condense a conversation to bullet points. Returns "" if the gateway is down."""
    return _llm.complete([
        {"role": "system",
         "content": "Summarise the following conversation. Keep all names, dates, "
                    "decisions, tasks, and deadlines. Output bullet points only. "
                    "Do not add commentary."},
        {"role": "user", "content": text},
    ], temperature=0.3, timeout=40.0).strip()
