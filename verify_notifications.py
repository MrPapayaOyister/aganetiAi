"""Verify the proactive mail pipeline with a SYNTHETIC email (no live Gmail needed):
proactive_alert → notification(s) → task_proposal → approve → real task created."""
import sys
sys.path.insert(0, ".")
from backend import notifications, mail_intel
from tasks.store import create_task, get_all_tasks

UID = "user_1"
EMAIL = {
    "id": "synthtest-msg-001",
    "from_name": "Fatima Al Nuaimi", "from_email": "fatima@daralber.ae",
    "subject": "Ramadan food-parcel campaign — approvals needed by Thursday",
    "body_text": ("Assalamu alaikum. We need your sign-off on the vendor quote for 5,000 "
                  "Ramadan food parcels, and please confirm the distribution schedule for the "
                  "Al Quoz labour camps by Thursday so we can brief the volunteers."),
}

def main():
    # clean any prior run
    print("=== proactive_alert on synthetic email (fast-7b summarize + task detect) ===")
    mail_intel.proactive_alert(UID, EMAIL)
    notifs = notifications.list_notifications(UID)
    mail_n = [n for n in notifs if n.get("kind") == "mail" and n.get("entity", {}).get("message_id") == EMAIL["id"]]
    task_n = [n for n in notifs if n.get("kind") == "task_proposal" and EMAIL["subject"] in (n.get("entity", {}).get("subject") or "")]
    print(f"mail notification: {len(mail_n)} | title: {mail_n[0]['title'] if mail_n else None}")
    if mail_n: print(f"  summary: {mail_n[0]['body']}")
    print(f"task proposal: {len(task_n)} | title: {task_n[0]['title'] if task_n else None}")

    # simulate the approve endpoint: create the task from the proposal entity
    created = False
    if task_n:
        ent = task_n[0]["entity"]
        before = len(get_all_tasks(UID, status="pending"))
        create_task(UID, title=ent["title"], source="email", priority=(ent.get("priority") or "Medium").lower())
        after = len(get_all_tasks(UID, status="pending"))
        created = after > before
        notifications.set_acted(UID, task_n[0]["id"])
        print(f"approve → task created: {created} (pending {before}→{after})")

    # dedup: re-run should NOT create duplicate notifications
    n_before = len(notifications.list_notifications(UID))
    mail_intel.proactive_alert(UID, EMAIL)
    n_after = len(notifications.list_notifications(UID))
    print(f"dedup on re-run: {n_before}=={n_after} -> {n_before == n_after}")

    ok = bool(mail_n) and bool(task_n) and created and (n_before == n_after)
    print("PIPELINE VERIFY:", "PASS" if ok else "PARTIAL/FAIL")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
