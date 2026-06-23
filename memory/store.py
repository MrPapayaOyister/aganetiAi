import os
import json
from datetime import datetime
from config.settings import MEMORY_DIR

def _get_path(session_id: str) -> str:
    return str(MEMORY_DIR / f"{session_id}.json")

def _get_state_path(session_id: str) -> str:
    return str(MEMORY_DIR / f"{session_id}_state.json")

def load_history(session_id: str) -> list:
    path = _get_path(session_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Chat history file {path} is corrupt. Error: {e}")
        return []

def append_message(user_id: str, role: str, content: str):
    history = load_history(user_id)
    history.append({
        "role": role,
        "content": content,
        "timestamp": datetime.utcnow().isoformat()
    })
    path = _get_path(user_id)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    summarize_old_history(user_id)

def save_message(session_id: str, role: str, content: str):
    append_message(session_id, role, content)

def save_session_state(session_id: str, state: dict):
    path = _get_state_path(session_id)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def load_session_state(session_id: str) -> dict:
    path = _get_state_path(session_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Session state file {path} is corrupt. Error: {e}")
        return {}

def _get_summaries_path(user_id: str) -> str:
    return str(MEMORY_DIR / user_id / "summaries.json")

def summarize_old_history(user_id: str):
    from memory.summarizer import call_llm_summarize
    history = load_history(user_id)
    if len(history) <= 20:
        return
        
    entries_to_summarize = history[0:-5]
    formatted_text = "".join(f"{entry['role']}: {entry['content']}\n" for entry in entries_to_summarize)
    
    summary = call_llm_summarize(formatted_text)
    if not summary:
        return
        
    summaries_path = _get_summaries_path(user_id)
    os.makedirs(os.path.dirname(summaries_path), exist_ok=True)
    
    summaries = []
    if os.path.exists(summaries_path):
        try:
            with open(summaries_path, "r", encoding="utf-8") as f:
                summaries = json.load(f)
                if not isinstance(summaries, list):
                    summaries = []
        except Exception:
            summaries = []
            
    new_summary_entry = {
        "summary": summary,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "covered_turns": len(entries_to_summarize)
    }
    summaries.append(new_summary_entry)
    
    with open(summaries_path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2)
        
    remaining_history = history[-5:]
    path = _get_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(remaining_history, f, indent=2)

    # Trigger background fact extraction (non-blocking)
    import threading
    from memory.long_term import extract_and_store
    threading.Thread(target=extract_and_store, args=(user_id,), daemon=True).start()
