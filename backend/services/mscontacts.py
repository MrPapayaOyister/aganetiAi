"""
Outlook contacts + organisation directory over Microsoft Graph.

Async twin of `backend/services/gcontacts.py`, returning the same contact shape
(id/name/email/phone/company/job_title/source) so contact resolution in the tool
layer is provider-agnostic.

Personal contacts come from /me/contacts; colleagues from /users (the tenant
directory), which is what makes "schedule a meeting with Akshay" resolve for
work accounts. Never logs tokens.
"""
from __future__ import annotations

import logging

from backend.services.provider_tokens import get_ms_headers, MS_CONFIGURED
from backend.services.http_client import api_request

log = logging.getLogger("aria.mscontacts")

GRAPH = "https://graph.microsoft.com/v1.0"


def _parse_contact(c: dict) -> dict:
    emails = c.get("emailAddresses") or []
    phones = (c.get("businessPhones") or []) + (c.get("homePhones") or [])
    mobile = c.get("mobilePhone")
    return {
        "id": c.get("id", ""),
        "name": c.get("displayName") or (emails[0].get("name") if emails else "") or "",
        "email": (emails[0].get("address") if emails else None),
        "phone": mobile or (phones[0] if phones else None),
        "photo_url": None,
        "company": c.get("companyName"),
        "job_title": c.get("jobTitle"),
        "source": "outlook_contacts",
    }


def _parse_directory_user(u: dict) -> dict:
    return {
        "id": u.get("id", ""),
        "name": u.get("displayName", ""),
        "email": u.get("mail") or u.get("userPrincipalName"),
        "phone": u.get("mobilePhone") or ((u.get("businessPhones") or [None])[0]),
        "photo_url": None,
        "company": u.get("companyName"),
        "job_title": u.get("jobTitle"),
        "source": "m365_directory",
    }


def _dedupe_sort(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for c in rows:
        key = (c.get("email") or "").lower()
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(c)
    out.sort(key=lambda c: (c.get("name") or "￿").lower())
    return out


async def get_contacts(user_id: str, max_results: int = 50) -> list[dict]:
    """Personal contacts plus tenant directory users, deduped + sorted by name."""
    if not MS_CONFIGURED:
        return []
    headers = await get_ms_headers(user_id)
    rows: list[dict] = []

    r = await api_request("GET", f"{GRAPH}/me/contacts", headers=headers, params={
        "$top": max_results,
        "$select": "id,displayName,emailAddresses,businessPhones,homePhones,mobilePhone,"
                   "companyName,jobTitle",
    })
    if r.status_code == 200:
        rows += [_parse_contact(c) for c in (r.json().get("value") or [])]
    else:
        log.info("graph contacts read failed for %s: %s", user_id, r.status_code)

    # Directory is optional — a personal account or a tenant that blocks User.Read.All
    # returns 403; that must not sink the personal-contacts result.
    d = await api_request("GET", f"{GRAPH}/users", headers=headers, params={
        "$top": max_results,
        "$select": "id,displayName,mail,userPrincipalName,mobilePhone,businessPhones,"
                   "companyName,jobTitle",
    })
    if d.status_code == 200:
        rows += [_parse_directory_user(u) for u in (d.json().get("value") or [])]

    return _dedupe_sort(rows)


async def search_contacts(user_id: str, query: str) -> list[dict]:
    """Search contacts + directory by name or email fragment."""
    if not MS_CONFIGURED or not query.strip():
        return []
    headers = await get_ms_headers(user_id)
    q = query.strip().replace("'", "''")     # OData string escaping
    rows: list[dict] = []

    r = await api_request("GET", f"{GRAPH}/me/contacts", headers=headers, params={
        "$filter": f"startswith(displayName,'{q}')",
        "$top": 10,
        "$select": "id,displayName,emailAddresses,businessPhones,homePhones,mobilePhone,"
                   "companyName,jobTitle",
    })
    if r.status_code == 200:
        rows += [_parse_contact(c) for c in (r.json().get("value") or [])]

    d = await api_request("GET", f"{GRAPH}/users", headers=headers, params={
        "$filter": f"startswith(displayName,'{q}') or startswith(mail,'{q}')",
        "$top": 10,
        "$select": "id,displayName,mail,userPrincipalName,mobilePhone,businessPhones,"
                   "companyName,jobTitle",
    })
    if d.status_code == 200:
        rows += [_parse_directory_user(u) for u in (d.json().get("value") or [])]

    return _dedupe_sort(rows)


async def get_contact_by_email(user_id: str, email: str) -> dict | None:
    hits = await search_contacts(user_id, email)
    for h in hits:
        if (h.get("email") or "").lower() == email.lower():
            return h
    return hits[0] if hits else None
