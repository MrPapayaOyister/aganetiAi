"""
UserContext — the single object that flows through every request.
Built once per request from the validated JWT + a lightweight DB lookup.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderContext:
    provider: str           # 'google' | 'microsoft'
    email: str
    scopes: list[str]
    token_expiry: Optional[str] = None


@dataclass
class UserContext:
    """
    Immutable request-scoped user context.
    Injected via FastAPI Depends(get_current_user).
    """
    user_id: str                    # Supabase auth.users.id (UUID)
    email: str
    display_name: str
    plan: str                       # 'free' | 'pro' | 'team' | 'enterprise'
    features: list[str] = field(default_factory=list)   # enabled feature flags
    providers: list[ProviderContext] = field(default_factory=list)

    # ── Capability checks ─────────────────────────────────────

    def has_feature(self, flag: str) -> bool:
        return flag in self.features

    def has_provider(self, name: str) -> bool:
        return any(p.provider == name for p in self.providers)

    def has_scope(self, provider: str, scope: str) -> bool:
        for p in self.providers:
            if p.provider == provider and scope in p.scopes:
                return True
        return False

    def available_tools(self) -> list[str]:
        """Return list of tool names this user is authorised to use."""
        tools = ["chat", "tasks_read", "tasks_write", "files_upload", "files_search"]

        if self.has_feature("voice"):
            tools += ["voice_input", "voice_output"]

        if self.has_feature("analytics") or self.plan in ("pro", "team", "enterprise"):
            tools.append("analytics")

        # Google tools
        if self.has_provider("google"):
            if self.has_scope("google", "https://www.googleapis.com/auth/gmail.readonly"):
                tools.append("gmail_read")
            if self.has_scope("google", "https://www.googleapis.com/auth/gmail.send"):
                tools.append("gmail_send")
            if self.has_scope("google", "https://www.googleapis.com/auth/calendar.readonly"):
                tools.append("gcal_read")
            if self.has_scope("google", "https://www.googleapis.com/auth/calendar.events"):
                tools.append("gcal_write")

        # Microsoft tools
        if self.has_provider("microsoft"):
            if self.has_scope("microsoft", "Mail.Read"):
                tools.append("m365_mail_read")
            if self.has_scope("microsoft", "Mail.Send"):
                tools.append("m365_mail_send")
            if self.has_scope("microsoft", "Calendars.Read"):
                tools.append("m365_cal_read")
            if self.has_scope("microsoft", "Calendars.ReadWrite"):
                tools.append("m365_cal_write")
            if self.has_scope("microsoft", "Contacts.Read"):
                tools.append("m365_contacts_read")

        if self.has_feature("agent_inbox") or self.plan in ("team", "enterprise"):
            tools.append("agent_messaging")

        return tools

    def system_prompt_context(self) -> str:
        """Render user identity + capabilities into a system-prompt block."""
        provider_lines = []
        for p in self.providers:
            caps = []
            if "gmail_read" in self.available_tools() and p.provider == "google":
                caps.append("read/send Gmail")
            if "m365_mail_read" in self.available_tools() and p.provider == "microsoft":
                caps.append("read/send Outlook")
            if "gcal_read" in self.available_tools() and p.provider == "google":
                caps.append("Google Calendar")
            if "m365_cal_read" in self.available_tools() and p.provider == "microsoft":
                caps.append("Outlook Calendar")
            provider_lines.append(
                f"  - {p.provider.title()} ({p.email}): {', '.join(caps) or 'connected, no extra scopes'}"
            )

        unavailable = []
        if not self.has_provider("google") and not self.has_provider("microsoft"):
            unavailable.append("email and calendar (no provider connected)")
        if not self.has_feature("voice"):
            unavailable.append("voice (plan upgrade required)")

        ctx = f"""--- USER CONTEXT ---
Name: {self.display_name}
Email: {self.email}
Plan: {self.plan}
Connected providers:
{chr(10).join(provider_lines) if provider_lines else "  None — chat and file features only"}
Available tools: {', '.join(self.available_tools())}
{"Unavailable: " + ', '.join(unavailable) if unavailable else ""}
--- END USER CONTEXT ---

You are Aria, a personal AI assistant. You have access ONLY to the tools listed above.
If the user asks you to do something that requires a tool not listed, tell them what
they need to connect or enable — do not pretend the capability exists.
"""
        return ctx.strip()
