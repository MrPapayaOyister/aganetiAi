import json
from openai import OpenAI
from config.settings import LLM_BASE_URL

# Initialize a separate OpenAI client to avoid circular dependencies
client = OpenAI(base_url=LLM_BASE_URL, api_key="local-dev")

SYSTEM_PROMPT = """You are a task intent classifier. Analyze the user message and determine if it expresses an intention to do something, a commitment, a reminder need, or a to-do item.

Respond ONLY with valid JSON in this exact format:
{"has_task": true, "title": "short task title", "priority": "low|medium|high", "due_date": "YYYY-MM-DD or null"}

If no task intent is detected, respond ONLY with:
{"has_task": false}

Examples of task intent:
- "I need to send the report to Ahmed by Friday" → has_task: true
- "I should review the Q3 proposal tomorrow" → has_task: true  
- "Remind me to call Sarah" → has_task: true

Examples of NO task intent:
- "What's on my calendar today?" → has_task: false
- "Summarize my emails" → has_task: false
- "Thanks" → has_task: false"""

def detect_task_intent(user_message: str) -> dict | None:
    """
    Analyzes the user message for task-related intentions.
    Returns the parsed intent dictionary if a task is detected, otherwise returns None.
    Fails silently in case of any exceptions or malformed JSON.
    """
    try:
        response = client.chat.completions.create(
            model="local-model",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.0,
            # Constrain the model to emit valid JSON. llama.cpp's server enforces this
            # via a grammar, removing most malformed-output cases. The markdown-fence
            # stripping below is kept as a defensive fallback for older server builds.
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        
        # Clean markdown code fences if output by the model
        if content.startswith("```"):
            lines = content.splitlines()
            if len(lines) >= 3:
                content = "\n".join(lines[1:-1]).strip()
            else:
                content = content.replace("```json", "").replace("```", "").strip()
        
        result = json.loads(content)
        if not isinstance(result, dict):
            return None
        
        if not result.get("has_task"):
            return None
        
        # Validate all required fields are present and non-empty
        title = result.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            print(f"Warning: detect_task_intent returned has_task=true but missing/empty title: {result}")
            return None
        
        # Normalise the dict to guarantee expected keys with safe defaults
        return {
            "has_task": True,
            "title": title.strip(),
            "priority": result.get("priority", "medium"),
            "due_date": result.get("due_date"),
        }
    except Exception as e:
        # Fail silently — never crash the chat flow
        print(f"Warning: detect_task_intent failed: {e}")
        return None
