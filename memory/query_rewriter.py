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
    url = "http://localhost:8080/v1/chat/completions"
    system_prompt = (
        "You are a search query optimizer. Rewrite vague follow-up questions into \n"
        "standalone keyword-rich search queries. Output ONLY the rewritten query. \n"
        "No punctuation at the end. No quotes. No explanation."
    )
    user_prompt = f"Conversation context:\n{context}\n\nFollow-up question: {current_message}\nRewritten standalone query:"
    payload = {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 60,
        "temperature": 0.1
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, json=payload)
            if response.status_code == 200:
                data = response.json()
                rewritten = data["choices"][0]["message"]["content"]
                rewritten = rewritten.strip().strip("'\"")
                if rewritten == "" or len(rewritten) > 100:
                    return current_message
                return rewritten
    except Exception as e:
        print(f"Error in query rewriter: {e}")
    return current_message
