import re
import json
import httpx
from datetime import datetime, timezone
from backend.service_auth import internal_headers  # Phase 0: auth for internal self-calls

BASE_URL = "http://127.0.0.1:8000"

def extract_action(llm_reply: str) -> tuple[dict | None, str]:
    """
    Extracts the structured [ACTION:{...}] tag from the end of the LLM reply.
    """
    pattern = r'\[ACTION:(\{.*?\})\]\s*$'
    match = re.search(pattern, llm_reply, re.DOTALL)
    if not match:
        return (None, llm_reply)
        
    try:
        json_str = match.group(1)
        parsed_dict = json.loads(json_str)
        clean_reply = llm_reply[:match.start()].strip()
        return (parsed_dict, clean_reply)
    except Exception:
        return (None, llm_reply)

def execute_action(action: dict, user_id: str) -> str:
    """
    Routes the action by type and calls the appropriate API endpoints.
    """
    action_type = action.get("type")
    if not action_type:
        return ""
        
    try:
        # Create Task
        if action_type == "create_task":
            url = f"{BASE_URL}/tasks"
            due_val = action.get("due")
            payload = {
                "title": action.get("title", "Untitled task"),
                "priority": action.get("priority", "medium"),
                "due_date": due_val if due_val != "null" and due_val is not None else None,
                "source": "chat",
                "user_id": user_id
            }
            response = httpx.post(url, json=payload, timeout=10.0, headers=internal_headers(user_id))
            if response.status_code in (200, 201):
                return "\n\n✅ Task created."
            return "\n\n⚠️ Task creation failed — please try again."
            
        # Complete Task
        elif action_type == "complete_task":
            url = f"{BASE_URL}/tasks/complete_by_title"
            payload = {"title": action.get("title", ""), "user_id": user_id}
            response = httpx.post(url, json=payload, timeout=10.0, headers=internal_headers(user_id))
            if response.status_code == 200:
                return "\n\n✅ Task marked as done."
            elif response.status_code == 404:
                return "\n\n⚠️ Couldn't find that task. Use /tasks to check the title."
            return "\n\n⚠️ Could not complete task — please try again."
            
        # Draft Email
        elif action_type == "draft_email":
            url = f"{BASE_URL}/draft_email"
            payload = {
                "to": action.get("to", ""),
                "subject": action.get("subject", ""),
                "body": action.get("body", ""),
                "user_id": user_id
            }
            response = httpx.post(url, json=payload, timeout=10.0, headers=internal_headers(user_id))
            if response.status_code == 200:
                return "\n\n📧 Draft queued for your approval in the approval queue."
            return "\n\n⚠️ Email draft failed — please try again."
            
        # Schedule Meeting
        elif action_type == "schedule_meeting":
            url = f"{BASE_URL}/schedule_meeting"
            payload = {
                "title": action.get("title", "Meeting"),
                "with": action.get("with", ""),
                "time": action.get("time", ""),
                "user_id": user_id
            }
            response = httpx.post(url, json=payload, timeout=10.0, headers=internal_headers(user_id))
            if response.status_code == 200:
                return "\n\n📅 Meeting scheduled and calendar event created."
            return "\n\n⚠️ Could not schedule meeting — please check the details."
            
    except Exception as e:
        print(f"Exception executing action: {e}")
        if action_type == "create_task":
            return "\n\n⚠️ Task creation failed — please try again."
        elif action_type == "complete_task":
            return "\n\n⚠️ Could not complete task — please try again."
        elif action_type == "draft_email":
            return "\n\n⚠️ Email draft failed — please try again."
        elif action_type == "schedule_meeting":
            return "\n\n⚠️ Could not schedule meeting — please check the details."
            
    return ""

def parse_and_execute_action(llm_reply: str, user_id: str) -> str:
    action, clean_reply = extract_action(llm_reply)
    if action is None:
        return llm_reply
    outcome = execute_action(action, user_id)
    return clean_reply + outcome
