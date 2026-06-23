import httpx

def call_llm_summarize(text: str) -> str:
    url = "http://localhost:8080/v1/chat/completions"
    payload = {
        "model": "local-model",
        "messages": [
            {
                "role": "system",
                "content": "Summarise the following conversation. Keep all names, dates, decisions, tasks, and deadlines. Output bullet points only. Do not add commentary."
            },
            {"role": "user", "content": text}
        ],
        "temperature": 0.3
    }
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        if response.status_code == 200:
            data = response.json()
            choices = data.get("choices")
            if choices and len(choices) > 0:
                content = choices[0].get("message", {}).get("content")
                if content:
                    return content.strip()
    except Exception as e:
        print(f"Error calling LLM for summarization: {e}")
    return ""
