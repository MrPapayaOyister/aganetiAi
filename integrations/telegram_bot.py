"""
Telegram Bot Integration Module

This module handles all Telegram bot operations using aiogram v3.
It runs concurrently with the FastAPI backend and forwards authorized incoming
messages to the local `/chat` endpoint. It is fully documented and isolated
so it can be easily extracted into a standalone service if needed.
"""

import sys
import os
import httpx
import time
import json as _json
import re
import asyncio
from aiogram import Bot, Dispatcher, Router, types, F
from integrations.whisper_transcriber import transcribe_audio
from integrations.tts import synthesize_speech
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from reports.email_digest import get_digest_for_user
from integrations.contacts import (
    resolve_contact, create_contact,
    update_contact, format_contact_for_display
)

# Resolve project root for settings import
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TTS_ENABLED
from config.users import get_user_by_telegram_id, USERS, get_user_name
from integrations.agent_inbox import send_message, get_pending_messages, resolve_message, reject_message
from tasks.store import find_task_by_title, create_task, get_conn

# Expose Bot at module level
bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

async def send_message_to_user(user_id: str, text: str):
    """Send a Telegram message to a user by their user_id."""
    chat_id = None
    try:
        from config.users import USERS
        chat_id = USERS.get(user_id, {}).get("telegram_chat_id")
    except (ImportError, ModuleNotFoundError):
        pass
    
    if not chat_id:
        from config.settings import TELEGRAM_CHAT_ID
        chat_id = TELEGRAM_CHAT_ID
        
    if not chat_id:
        print(f"[WARN] No telegram_chat_id for {user_id}, cannot send message")
        return
        
    await bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode="Markdown"
    )

# Initialize Dispatcher and Router
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Cache mapping 8-character short_id -> full_uuid
_task_id_cache = {}

UNAUTHORIZED_MSG = (
    "⛔ You are not authorised to use this assistant.\n"
    "Contact the administrator to get access."
)

def resolve_user_id(chat_id: int) -> str | None:
    """
    Identity gate: resolve a registered user_id from a Telegram chat id.
    Returns None for unknown chat ids — callers must block unauthorised users.
    """
    user = get_user_by_telegram_id(chat_id)
    return user["user_id"] if user else None

def is_authorized(chat_id: int) -> bool:
    """Backwards-compatible check — now registry-based (any registered user is authorised)."""
    return resolve_user_id(chat_id) is not None

@router.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    Handles the /start command.
    Welcomes the user to the Workspace Assistant.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return
    await message.answer("✨ Workspace Assistant is online. How can I help you today?")

@router.message(Command("tasks"))
async def tasks_command_handler(message: Message) -> None:
    """
    Handles the /tasks command.
    Fetches the pending tasks summary from the backend and prints tasks with their short IDs.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    url_summary = f"http://127.0.0.1:8000/tasks/summary?user_id={user_id}"
    url_list = f"http://127.0.0.1:8000/tasks?status=pending&user_id={user_id}"

    try:
        async with httpx.AsyncClient() as client:
            res_summary = await client.get(url_summary)
            if res_summary.status_code != 200:
                await message.answer(f"Error: Backend returned status code {res_summary.status_code}.")
                return

            summary_data = res_summary.json()
            summary = summary_data.get("summary", "")

            if summary == "No pending tasks.":
                await message.answer("✅ You have no pending tasks right now.")
                return

            # Fetch detailed task list to map short IDs and cache full UUIDs
            res_list = await client.get(url_list)
            if res_list.status_code == 200:
                tasks = res_list.json()
                lines = ["Pending Tasks:"]
                for t in tasks:
                    full_uuid = t["id"]
                    short_id = full_uuid[:8]
                    _task_id_cache[short_id] = full_uuid

                    line = f"- [{t['priority']}] {t['title']}"
                    if t.get("due_date"):
                        line += f" (due: {t['due_date']})"
                    line += f" [ID: {short_id}]"
                    lines.append(line)

                await message.answer("\n".join(lines))
            else:
                # Fallback to direct summary representation if detailed list fails
                await message.answer(summary)
    except Exception as e:
        await message.answer(f"Connection error: Could not reach task API. Details: {e}")

@router.message(Command("addtask"))
async def addtask_command_handler(message: Message, command: CommandObject) -> None:
    """
    Handles the /addtask command.
    Allows creating new tasks directly with optional flags like due:YYYY-MM-DD and priority:high.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    args = command.args
    if not args:
        await message.answer("Usage: /addtask <task title> [due:YYYY-MM-DD] [priority:high]")
        return

    # Parse inline flags using regex
    due_match = re.search(r"\bdue:(\S+)", args)
    priority_match = re.search(r"\bpriority:(\S+)", args)

    due_date = None
    priority = "medium"

    if due_match:
        due_date = due_match.group(1)
        args = re.sub(r"\bdue:\S+", "", args)

    if priority_match:
        priority = priority_match.group(1)
        args = re.sub(r"\bpriority:\S+", "", args)

    title = re.sub(r"\s+", " ", args).strip()

    if not title:
        await message.answer("Usage: /addtask <task title> [due:YYYY-MM-DD] [priority:high]")
        return

    url = "http://127.0.0.1:8000/tasks"
    payload = {
        "title": title,
        "priority": priority,
        "due_date": due_date,
        "source": "telegram",
        "user_id": user_id
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            if response.status_code == 201:
                await message.answer(f"✅ Task added: **{title}**", parse_mode="Markdown")
            else:
                await message.answer(f"Error: Backend returned status code {response.status_code}.")
    except Exception as e:
        await message.answer(f"Connection error: Could not reach task API. Details: {e}")

@router.message(Command("done"))
async def done_command_handler(message: Message, command: CommandObject) -> None:
    """
    Handles the /done command.
    Marks a task as completed using its short ID cache or full UUID.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    task_id = command.args
    if not task_id:
        await message.answer("Usage: /done <task_id> — get IDs from /tasks")
        return

    task_id = task_id.strip()

    # Look up full UUID in cache; fallback to using input directly
    full_uuid = _task_id_cache.get(task_id, task_id)

    url = f"http://127.0.0.1:8000/tasks/{full_uuid}?user_id={user_id}"
    payload = {"status": "done"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(url, json=payload)
            if response.status_code == 200:
                await message.answer("✅ Task marked as done!")
            elif response.status_code == 404:
                await message.answer("❌ Task not found. Use /tasks to see valid IDs.")
            else:
                await message.answer(f"Error: Backend returned status code {response.status_code}.")
    except Exception as e:
        await message.answer(f"Connection error: Could not reach task API. Details: {e}")

@router.message(F.voice)
async def telegram_voice_handler(message: Message) -> None:
    """
    Handles voice messages by downloading, transcribing them off-thread,
    and routing the transcript text through the /chat LLM workflow.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    await message.reply("🎙️ Processing your voice message...")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_dir = os.path.join(project_root, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"voice_{message.voice.file_id}.ogg")

    try:
        file_info = await bot.get_file(message.voice.file_id)
        await bot.download_file(file_info.file_path, destination=temp_path)

        transcript = await asyncio.to_thread(transcribe_audio, temp_path)

        if transcript in ["[No speech detected]", "[Transcription failed]"]:
            await message.reply("❌ Sorry, I couldn't understand that voice message. Please try again or type your message.")
            return

        await message.reply(f"📝 I heard: _{transcript}_", parse_mode="Markdown")

        url = "http://127.0.0.1:8000/chat"
        payload = {
            "message": transcript,
            "session_id": user_id,
            "user_id": user_id,
            "stream": True
        }

        ack_msg = await message.answer("⏳ Transcribed. Thinking...")

        collected    = ""
        last_edit_at = 0.0
        token_count  = 0

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", url, json=payload, timeout=90.0) as resp:
                    if resp.status_code != 200:
                        collected = f"Error: Backend returned status code {resp.status_code}."
                    else:
                        async for raw_line in resp.aiter_lines():
                            if not raw_line or raw_line.strip() == "data: [DONE]":
                                continue
                            try:
                                line = raw_line.removeprefix("data: ").strip()
                                data  = _json.loads(line)
                                token = data["choices"][0]["delta"].get("content", "")
                            except Exception:
                                continue

                            collected   += token
                            token_count += 1

                            # Edit every 10 tokens, max once per second
                            if token_count % 10 == 0:
                                now = time.time()
                                if now - last_edit_at >= 1.0 and collected.strip():
                                    try:
                                        # Strip action tags dynamically
                                        display_text = collected.strip()
                                        if "[ACTION:" in display_text:
                                            display_text = display_text.split("[ACTION:")[0].strip()
                                        if display_text:
                                            await ack_msg.edit_text(display_text + " ▌")
                                            last_edit_at = now
                                    except Exception:
                                        pass  # Telegram rejects edits on identical text — safe to ignore
        except Exception as e:
            collected = f"Connection error: Could not reach chat API. Details: {e}"

        # Final edit — remove cursor, show complete response
        reply = collected.strip()
        if "[ACTION:" in reply:
            reply = reply.split("[ACTION:")[0].strip()
        reply = reply or "⚠️ No response received."
        
        try:
            await ack_msg.edit_text(reply)
        except Exception:
            pass

        if TTS_ENABLED:
            tts_path = os.path.join(temp_dir, f"tts_{message.voice.file_id}.wav")
            try:
                tts_success = await asyncio.to_thread(synthesize_speech, reply, tts_path)
                if tts_success:
                    voice_file = FSInputFile(tts_path)
                    await message.reply_voice(voice=voice_file)
            except Exception as tts_err:
                print(f"TTS response generation failed: {tts_err}")
            finally:
                if os.path.exists(tts_path):
                    try:
                        os.remove(tts_path)
                    except Exception as ex:
                        print(f"Error removing TTS temp file: {ex}")

    except Exception as e:
        print(f"Error handling voice message: {e}")
        await message.reply("❌ Error processing voice message.")
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception as ex:
                print(f"Error removing temp file: {ex}")

@router.message(Command("disable_digest", "disabledigest"))
async def disable_digest_command_handler(message: Message) -> None:
    """
    Handles /disable_digest or /disabledigest.
    Disables the daily scheduled email digest for the current user.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    from pathlib import Path
    flag_path = Path(f"email_store/{user_id}/digest_disabled")
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.touch()
    await message.answer("📬 Daily email digest has been disabled. ✅")

@router.message(Command("enable_digest", "enabledigest"))
async def enable_digest_command_handler(message: Message) -> None:
    """
    Handles /enable_digest or /enabledigest.
    Enables the daily scheduled email digest for the current user.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    from pathlib import Path
    flag_path = Path(f"email_store/{user_id}/digest_disabled")
    if flag_path.exists():
        flag_path.unlink()
    await message.answer("📬 Daily email digest has been enabled (scheduled for 8:00 AM). ✅")

@router.message(F.audio)
async def audio_meeting_handler(message: Message) -> None:
    """
    Handles audio file uploads (not voice notes) as meeting recordings.
    Routes through /meeting/transcribe_path for full meeting-brief extraction.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    ack = await message.answer("🎙️ Detected an audio file — processing as meeting recording...")

    audio = message.audio
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_dir = os.path.join(project_root, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    ext = ".mp3" if (audio.mime_type or "").startswith("audio/mpeg") else ".ogg"
    temp_path = os.path.join(temp_dir, f"meet_{audio.file_id}{ext}")

    try:
        file_info = await bot.get_file(audio.file_id)
        await bot.download_file(file_info.file_path, destination=temp_path)

        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://127.0.0.1:8000/meeting/transcribe_path",
                json={"user_id": user_id, "path": temp_path,
                      "title": audio.file_name or "Meeting Recording"},
                timeout=300.0,
            )
        data = r.json() if r.status_code == 200 else {}
        if data.get("status") == "ok":
            await ack.edit_text(
                f"✅ Meeting processed.\n"
                f"• {data.get('tasks_created', 0)} task(s) created\n"
                f"• Draft follow-up: {'queued ✉️' if data.get('draft_queued') else 'not needed'}"
            )
        else:
            await ack.edit_text(f"⚠️ Processing failed: {data.get('message', 'unknown error')}")
    except Exception as e:
        await ack.edit_text(f"⚠️ Meeting recorder error: {e}")
    finally:
        try:
            import os as _os
            _os.unlink(temp_path)
        except Exception:
            pass


@router.message(F.document | F.photo)
async def document_message_handler(message: Message):
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    status_msg = await message.answer("📄 Reading your document...")

    from integrations.document_handler import handle_document_upload
    response = await handle_document_upload(bot, message, user_id, status_msg)

    await status_msg.edit_text(response, parse_mode="Markdown")

@router.message()
async def telegram_message_handler(message: Message) -> None:
    """
    Core message handler that filters unauthorized users,
    shows typing indicator, forwards the prompt to the backend /chat API,
    and returns the LLM response.
    """
    user_id = resolve_user_id(message.chat.id)
    if user_id is None:
        await message.answer(UNAUTHORIZED_MSG)
        return
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    msg_lower = message.text.lower().strip() if message.text else ""

    # Check if user is asking a follow-up about a recently uploaded document
    from pathlib import Path
    import json
    session_file = Path(f"temp/uploads/{user_id}/last_document.json")
    DOC_FOLLOWUP_TRIGGERS = [
        "summarise it", "summarize it", "extract tasks", "find all names",
        "draft a reply", "add to memory", "what does it say",
        "tell me more", "what are the deadlines", "who is mentioned"
    ]

    if session_file.exists() and any(t in msg_lower for t in DOC_FOLLOWUP_TRIGGERS):
        doc_session = json.loads(session_file.read_text())
        # Check document is less than 2 hours old
        from datetime import datetime, timezone
        uploaded_at = datetime.fromisoformat(doc_session["uploaded_at"])
        age_hours = (datetime.now(timezone.utc) - uploaded_at).total_seconds() / 3600

        if age_hours < 2:
            from integrations.document_handler import analyse_document_stream
            status_msg = await message.answer("🔍 Analysing your document...")
            
            collected = ""
            last_edit_at = 0.0
            token_count = 0
            
            async for token in analyse_document_stream(
                doc_session["text"],
                doc_session["filename"],
                message.text
            ):
                collected += token
                token_count += 1
                
                if token_count % 10 == 0:
                    import time
                    now = time.time()
                    if now - last_edit_at >= 1.0 and collected.strip():
                        try:
                            display_text = collected.strip()
                            if "[ACTION:" in display_text:
                                display_text = display_text.split("[ACTION:")[0].strip()
                            if display_text:
                                await status_msg.edit_text(display_text + " ▌")
                                last_edit_at = now
                        except Exception:
                            pass
            
            final_result = collected.strip()
            if "[ACTION:" in final_result:
                final_result = final_result.split("[ACTION:")[0].strip()
            final_result = final_result or "⚠️ No response received."
            
            try:
                await status_msg.edit_text(final_result, parse_mode="Markdown")
            except Exception:
                pass
            return

    SCHEDULE_CREATE_TRIGGERS = [
        "remind me", "schedule a reminder", "every day at", "every morning",
        "every evening", "every monday", "every tuesday", "every wednesday",
        "every thursday", "every friday", "every weekend", "every weekday",
        "every hour", "send me every", "notify me every", "alert me every"
    ]

    SCHEDULE_LIST_TRIGGERS = [
        "my schedules", "show schedules", "list schedules",
        "what reminders", "my reminders", "active schedules"
    ]

    SCHEDULE_DELETE_TRIGGERS = [
        "delete schedule", "remove schedule", "cancel schedule",
        "stop reminder", "delete reminder", "remove reminder"
    ]

    # List schedules
    if any(t in msg_lower for t in SCHEDULE_LIST_TRIGGERS):
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:8000/schedule/list/{user_id}")
        await message.answer(r.json()["formatted"], parse_mode="Markdown")
        return

    # Delete schedule
    if any(t in msg_lower for t in SCHEDULE_DELETE_TRIGGERS):
        id_match = re.search(r'\b([a-f0-9]{8})\b', message.text)
        if id_match:
            sched_id = id_match.group(1)
            async with httpx.AsyncClient() as client:
                r = await client.delete(f"http://127.0.0.1:8000/schedule/{user_id}/{sched_id}")
            result = r.json()
            if result["status"] == "deleted":
                await message.answer(f"✅ Schedule `{sched_id}` deleted.")
            else:
                await message.answer(f"⚠️ Schedule `{sched_id}` not found. Use _\"my schedules\"_ to see your active schedule IDs.", parse_mode="Markdown")
        else:
            await message.answer("Please include the schedule ID. Say _\"my schedules\"_ to see your IDs, then _\"delete schedule XXXXXXXX\"_.", parse_mode="Markdown")
        return

    # Create schedule
    if any(t in msg_lower for t in SCHEDULE_CREATE_TRIGGERS):
        status_msg = await message.answer("⏰ Setting up your schedule...")
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://127.0.0.1:8000/schedule/create",
                json={"user_id": user_id, "text": message.text}
            )
        result = r.json()
        if result["status"] == "created":
            sched = result["schedule"]
            from scheduler.schedule_manager import CRON_LABELS
            human_time = CRON_LABELS.get(sched["cron_expression"], f"Custom: {sched['cron_expression']}")
            await status_msg.edit_text(
                f"✅ Schedule created!\n\n"
                f"*{human_time}* — {sched['action_type'].replace('_', ' ').title()}\n"
                f"ID: `{sched['id']}`\n\n"
                f"Say _\"my schedules\"_ to see all your schedules.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                "⚠️ I couldn't understand the schedule timing. Try: _\"remind me every day at 8am to check emails\"_",
                parse_mode="Markdown"
            )
        return

    DISABLE_TRIGGERS = [
        "disable daily digest", "disable email digest",
        "turn off daily digest", "turn off email digest",
        "stop daily digest", "stop email digest"
    ]
    ENABLE_TRIGGERS = [
        "enable daily digest", "enable email digest",
        "turn on daily digest", "turn on email digest",
        "start daily digest", "start email digest"
    ]

    from pathlib import Path

    if any(trigger in msg_lower for trigger in DISABLE_TRIGGERS):
        flag_path = Path(f"email_store/{user_id}/digest_disabled")
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.touch()
        await message.answer("📬 Daily email digest has been disabled. ✅")
        return

    if any(trigger in msg_lower for trigger in ENABLE_TRIGGERS):
        flag_path = Path(f"email_store/{user_id}/digest_disabled")
        if flag_path.exists():
            flag_path.unlink()
        await message.answer("📬 Daily email digest has been enabled (scheduled for 8:00 AM). ✅")
        return

    DIGEST_TRIGGERS = [
        "show me my emails", "email digest", "check emails",
        "any new emails", "what emails", "unread emails",
        "show emails", "email summary", "my inbox"
    ]

    if any(trigger in msg_lower for trigger in DIGEST_TRIGGERS):
        await message.answer("🔍 Checking your inbox...")
        digest = await asyncio.to_thread(get_digest_for_user, user_id, True)
        await message.answer(digest, parse_mode="Markdown")
        return   # do not pass to /chat endpoint

    # ── Document drafting intent (memo / proposal / SOP / one-pager / letter) ──
    # Checked before reports so "draft a memo" isn't swallowed by report triggers.
    DOC_TYPE_WORDS = {
        "one-pager": "one-pager", "one pager": "one-pager", "onepager": "one-pager",
        "memo": "memo", "proposal": "proposal", "sop": "sop", "procedure": "sop",
        "letter": "letter", "briefing": "brief", "brief": "brief",
    }
    DOC_VERBS = ("draft", "write", "create", "prepare", "compose")
    if any(v in msg_lower for v in DOC_VERBS) and any(w in msg_lower for w in DOC_TYPE_WORDS):
        import re as _re_doc
        doc_type = "memo"
        for w, t in DOC_TYPE_WORDS.items():
            if w in msg_lower:
                doc_type = t
                break
        topic = message.text
        m = _re_doc.search(r'\b(?:about|on|regarding|covering|for)\b\s+(.*)', message.text, _re_doc.IGNORECASE)
        if m:
            topic = m.group(1).strip()
        includes = []
        for kw, flag in (("calendar", "calendar"), ("task", "tasks"),
                         ("delegat", "agents"), ("memory", "memory"), ("agenda", "calendar")):
            if kw in msg_lower and flag not in includes:
                includes.append(flag)
        status_msg = await message.answer(f"📝 Drafting your {doc_type}...")
        try:
            from backend.documents import draft_document
            pdf_path = await asyncio.to_thread(draft_document, user_id, doc_type, topic, includes, None)
            from datetime import datetime
            from aiogram.types import FSInputFile
            await message.answer_document(
                document=FSInputFile(str(pdf_path)),
                caption=f"📄 Your {doc_type} — {datetime.now().strftime('%B %d, %Y')}",
            )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"⚠️ Drafting failed: {str(e)[:120]}")
        return   # do not pass to /chat endpoint

    REPORT_TRIGGERS = [
        "generate a report", "generate report", "make a report",
        "create a report", "give me a report", "send me a report",
        "daily summary", "weekly summary", "give me my report",
        "pdf report", "generate pdf", "my summary"
    ]

    if any(trigger in msg_lower for trigger in REPORT_TRIGGERS):
        status_msg = await message.answer("📊 Generating your report...")
        
        from reports.pdf_generator import parse_report_intent, generate_pdf
        intent = parse_report_intent(message.text)
        
        try:
            pdf_path = await asyncio.to_thread(
                generate_pdf,
                user_id,
                intent["sections"],
                intent["title"],
                intent["query"]
            )
            from datetime import datetime
            from aiogram.types import FSInputFile
            doc = FSInputFile(str(pdf_path))
            await message.answer_document(
                document=doc,
                caption=f"📄 *{intent['title']}* — {datetime.now().strftime('%B %d, %Y')}",
                parse_mode="Markdown"
            )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"⚠️ Report generation failed: {str(e)[:100]}")
        return   # do not pass to /chat endpoint

    REMEMBER_TRIGGERS = ["remember that", "note that", "save that", "log that"]
    LOOKUP_TRIGGERS   = ["what do i know about", "who is", "tell me about", "contact info for", "details on"]

    msg_lower = message.text.lower().strip() if message.text else ""

    # --- CONTACT SAVE INTENT ---
    # Pattern: "remember John is my lawyer" / "note that Ahmed is the CFO at Acme"
    if any(t in msg_lower for t in REMEMBER_TRIGGERS):
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        ack_msg = await message.answer("📇 Saving contact info...")
        # Pass full message to LLM (smart model) to extract name + role/notes
        # Use call_llm with a structured extraction prompt
        extract_prompt = [
            {"role": "system", "content": (
                "Extract contact information from the user's message. "
                "Return ONLY valid JSON with keys: full_name (required), "
                "email (optional), nickname (optional), role (optional), "
                "department (optional), notes (optional). "
                "Example: {\"full_name\": \"John Smith\", \"role\": \"lawyer\"}"
            )},
            {"role": "user", "content": message.text}
        ]
        extracted_json = ""
        from backend.main import call_llm
        async for chunk in call_llm(
            messages=extract_prompt,
            user_message=message.text,
            stream=False
        ):
            extracted_json = chunk

        try:
            import json as _json
            import re
            # Strip markdown code fences if LLM wraps in ```json
            clean = re.sub(r"```(?:json)?|```", "", extracted_json).strip()
            data = _json.loads(clean)
            if not data.get("full_name"):
                raise ValueError("No full_name extracted")

            # Check if contact already exists — update instead of duplicate
            existing = resolve_contact(data["full_name"])
            if existing:
                update_contact(existing["id"], **{k: v for k, v in data.items() if k != "full_name"})
                reply = f"✅ Updated contact: {format_contact_for_display({**existing, **data})}"
            else:
                contact = create_contact(**data)
                reply = f"✅ Saved contact:\n{format_contact_for_display(contact)}"
        except Exception as e:
            reply = f"⚠️ Couldn't parse contact info. Try: 'Remember John Smith is my lawyer at Acme Corp.'"

        await ack_msg.edit_text(reply, parse_mode="Markdown")
        return  # do not pass to /chat endpoint

    # --- CONTACT LOOKUP INTENT ---
    # Pattern: "who is Ahmed" / "what do I know about Sarah"
    if any(t in msg_lower for t in LOOKUP_TRIGGERS):
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        # Extract the name being queried — take text after the trigger phrase
        name_query = msg_lower
        for t in LOOKUP_TRIGGERS:
            if t in name_query:
                name_query = message.text[msg_lower.index(t) + len(t):].strip(" ?.")
                break
        contact = resolve_contact(name_query)
        if contact:
            await message.answer(
                format_contact_for_display(contact),
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                f"🔍 I don't have any saved info for *{name_query}*.\n"
                f"You can save them with: _'Remember {name_query} is their role at Company'_",
                parse_mode="Markdown"
            )
        return  # do not pass to /chat endpoint

    # ── Delegation intent detection ────────────────────────────────
    user_text = message.text or ""
    import re as _re
    _DELEGATE_PATTERN = _re.compile(
        r"delegate\s+(.+?)\s+to\s+(.+)",
        _re.IGNORECASE
    )
    _delegate_match = _DELEGATE_PATTERN.search(user_text)

    if _delegate_match:
        task_fragment = _delegate_match.group(1).strip()
        target_name   = _delegate_match.group(2).strip().lower()

        # Resolve target user by name
        target_user_id = None
        for uid, udata in USERS.items():
            if target_name in udata.get("name", "").lower() or target_name in uid.lower():
                target_user_id = uid
                break

        if not target_user_id or target_user_id == user_id:
            await message.answer("❓ I couldn't find that user. Try: 'delegate [task] to Akshay'")
            return

        # Find the task in the sender's task store
        task = find_task_by_title(user_id, task_fragment)
        if not task:
            await message.answer(
                f"❓ I couldn't find a task matching '{task_fragment}' in your list.\n"
                f"Use /tasks to see your current tasks."
            )
            return

        from_agent = USERS[user_id]["agent_id"]
        to_agent   = USERS[target_user_id]["agent_id"]

        send_message(
            from_agent = from_agent,
            to_agent   = to_agent,
            type       = "task_delegation",
            payload    = {
                "task_id":   task["id"],
                "title":     task["title"],
                "priority":  task["priority"],
                "notes":     task.get("notes", ""),
                "from_user": user_id,
                "from_name": get_user_name(user_id)
            }
        )

        target_name_display = get_user_name(target_user_id)
        await message.answer(
            f"📤 Delegation request sent to {target_name_display}.\n"
            f"📋 Task: *{task['title']}*\n"
            f"They will be notified shortly.",
            parse_mode="Markdown"
        )
        return
    # ── End delegation intent ───────────────────────────────────────

    # Send request to local `/chat` API endpoint with streaming enabled
    url = "http://127.0.0.1:8000/chat"
    payload = {
        "message": message.text,
        "session_id": user_id,
        "user_id": user_id,
        "stream": True
    }

    # 2. Immediate ACK — Telegram 30s clock stops here
    ack_msg = await message.answer("⏳")

    collected    = ""
    last_edit_at = 0.0
    token_count  = 0

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json=payload, timeout=90.0) as resp:
                if resp.status_code != 200:
                    collected = f"Error: Backend returned status code {resp.status_code}."
                else:
                    async for raw_line in resp.aiter_lines():
                        if not raw_line or raw_line.strip() == "data: [DONE]":
                            continue
                        try:
                            line = raw_line.removeprefix("data: ").strip()
                            data  = _json.loads(line)
                            token = data["choices"][0]["delta"].get("content", "")
                        except Exception:
                            continue

                        collected   += token
                        token_count += 1

                        # Edit every 10 tokens, max once per second
                        if token_count % 10 == 0:
                            now = time.time()
                            if now - last_edit_at >= 1.0 and collected.strip():
                                try:
                                    # Strip action tags dynamically during stream
                                    display_text = collected.strip()
                                    if "[ACTION:" in display_text:
                                        display_text = display_text.split("[ACTION:")[0].strip()
                                    if display_text:
                                        await ack_msg.edit_text(display_text + " ▌")
                                        last_edit_at = now
                                except Exception:
                                    pass  # Telegram rejects edits on identical text — safe to ignore
    except Exception as e:
        collected = f"Connection error: Could not reach chat API. Details: {e}"

    # 5. Final edit — remove cursor, show complete response
    final = collected.strip()
    if "[ACTION:" in final:
        final = final.split("[ACTION:")[0].strip()
    final = final or "⚠️ No response received."
    
    try:
        await ack_msg.edit_text(final)
    except Exception:
        pass

@router.callback_query()
async def handle_delegation_callback(callback: CallbackQuery):
    """Handles Accept/Reject button taps on delegation notifications."""
    data = callback.data or ""

    # ── Identity gate ───────────────────────────────────────────
    user = get_user_by_telegram_id(callback.message.chat.id)
    if not user:
        await callback.answer("⛔ Not authorised.")
        return
    user_id  = user["user_id"]
    agent_id = USERS[user_id]["agent_id"]
    # ── End identity gate ───────────────────────────────────────

    if data.startswith("delegate_accept:"):
        msg_id = data.split(":", 1)[1]

        # Fetch message payload from DB
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_messages WHERE id=?", (msg_id,)
            ).fetchone()

        if not row:
            await callback.answer("Message not found.")
            return

        import json as _json
        payload = _json.loads(row["payload"])

        # Create task in recipient's store
        create_task(
            user_id  = user_id,
            title    = payload["title"],
            priority = payload.get("priority", "medium"),
            notes    = payload.get("notes", ""),
            source   = "delegation"
        )

        # Resolve incoming message
        resolve_message(msg_id)

        # Send acceptance back to sender
        from_user = payload.get("from_user") or (next(iter(USERS)) if USERS else "")
        from_agent = USERS.get(from_user, {}).get("agent_id") if from_user else None
        if not from_agent:
            from_agent = next((u.get("agent_id") for u in USERS.values() if u.get("agent_id")), "")
        send_message(
            from_agent = agent_id,
            to_agent   = from_agent,
            type       = "delegation_accepted",
            payload    = {
                "task_id":   payload.get("task_id"),
                "title":     payload["title"],
                "from_user": user_id,
                "from_name": get_user_name(user_id)
            }
        )

        await callback.message.edit_text(
            f"✅ You accepted: *{payload['title']}*\n"
            f"Added to your task list.",
            parse_mode="Markdown"
        )
        await callback.answer("Task accepted ✅")

    elif data.startswith("delegate_reject:"):
        msg_id = data.split(":", 1)[1]

        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_messages WHERE id=?", (msg_id,)
            ).fetchone()

        if not row:
            await callback.answer("Message not found.")
            return

        import json as _json
        payload = _json.loads(row["payload"])

        reject_message(msg_id, reason="declined by recipient")

        from_user  = payload.get("from_user") or (next(iter(USERS)) if USERS else "")
        from_agent = USERS.get(from_user, {}).get("agent_id") if from_user else None
        if not from_agent:
            from_agent = next((u.get("agent_id") for u in USERS.values() if u.get("agent_id")), "")
        send_message(
            from_agent = agent_id,
            to_agent   = from_agent,
            type       = "delegation_rejected",
            payload    = {
                "task_id":   payload.get("task_id"),
                "title":     payload["title"],
                "reason":    "declined by recipient",
                "from_user": user_id,
                "from_name": get_user_name(user_id)
            }
        )

        await callback.message.edit_text(
            f"❌ You declined: *{payload['title']}*",
            parse_mode="Markdown"
        )
        await callback.answer("Task rejected ❌")

async def start_bot() -> None:
    """
    Initializes the Bot client and starts long polling.
    Designed to be run concurrently within FastAPI's lifespan events block.
    """
    if not bot:
        print("Warning: Bot client is not initialized because TELEGRAM_BOT_TOKEN is not set.")
        return

    try:
        print("🚀 Telegram Bot engine polling started...")
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Error starting Telegram bot: {e}")
