import httpx

def rewrite_query(recent_history: list[dict], current_message: str) -> str:
    vague_tokens = [
        "it", "that", "this", "he", "she", "they", "them", "there",
        "what about", "and also", "same", "his", "her", "the meeting",
        "the task", "the email"
    ]
    if len(current_message.split()) > 15:
        return current_message
    message_lower = current_message.lower()
    if not any(token in message_lower for token in vague_tokens):
        return current_message
    if not recent_history:
        return current_message
    context = "\n".join([f"{m['role']}: {m['content'][:100]}" for m in recent_history[-3:]])
    system_prompt = (
        "You are a search query optimizer. Rewrite vague follow-up questions into \n"
        "standalone keyword-rich search queries. Output ONLY the rewritten query. \n"
        "No punctuation at the end. No quotes. No explanation."
    )
    user_prompt = f"Conversation context:\n{context}\n\nFollow-up question: {current_message}\nRewritten standalone query:"
    from backend.services import llm as _llm
    rewritten = _llm.complete([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], max_tokens=60, temperature=0.1, timeout=15.0).strip().strip("'\"")
    if not rewritten or len(rewritten) > 100:
        return current_message
    return rewritten
