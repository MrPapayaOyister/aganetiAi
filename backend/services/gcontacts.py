"""
Google Contacts integration (per-user, via People API).

Replaces the old fake /contacts data with real personal + directory contacts.
Async over the shared httpx client; never logs tokens; mock fallback when
unconfigured.
"""
from __future__ import annotations

import logging

from backend.services.provider_tokens import get_google_headers, GOOGLE_CONFIGURED
from backend.services.http_client import google_request

log = logging.getLogger("aria.gcontacts")

PEOPLE = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,photos,organizations"

_MOCK = [{
    "id": "mockc1", "name": "Connect Google Contacts", "email": None, "phone": None,
    "photo_url": None, "company": None, "job_title": None, "source": "mock",
}]


def _parse_person(p: dict) -> dict:
    names = p.get("names") or [{}]
    emails = p.get("emailAddresses") or []
    phones = p.get("phoneNumbers") or []
    photos = p.get("photos") or []
    orgs = p.get("organizations") or [{}]
    return {
        "id": p.get("resourceName", ""),
        "name": names[0].get("displayName", "") if names else "",
        "email": emails[0].get("value") if emails else None,
        "phone": phones[0].get("value") if phones else None,
        "photo_url": photos[0].get("url") if photos else None,
        "company": orgs[0].get("name") if orgs else None,
        "job_title": orgs[0].get("title") if orgs else None,
        "source": "google_contacts",
    }


def _dedupe_sort(rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for c in rows:
        key = (c.get("email") or "").lower()
        if key:
            if key in seen:
                continue
            seen[key] = c
        out.append(c)
    out.sort(key=lambda c: (c.get("name") or "￿").lower())
    return out


async def get_google_contacts(user_id: str, max_results: int = 50) -> list[dict]:
    """Return real contacts (personal + workspace directory), deduped + sorted."""
    if not GOOGLE_CONFIGURED:
        return _MOCK
    headers = await get_google_headers(user_id)
    r = await google_request("GET", f"{PEOPLE}/people/me/connections", headers=headers, params={
        "personFields": PERSON_FIELDS, "pageSize": max_results,
        "sortOrder": "FIRST_NAME_ASCENDING",
    })
    contacts: list[dict] = []
    if r.status_code == 200:
        contacts = [_parse_person(p) for p in (r.json().get("connections") or [])]
    else:
        log.warning("people connections failed for %s: %s", user_id, r.status_code)

    # Fallback: pull workspace directory contacts too if the personal list is thin.
    if len(contacts) < 10:
        d = await google_request("GET", f"{PEOPLE}/people:listDirectoryPeople", headers=headers, params={
            "readMask": "names,emailAddresses",
            "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT",
            "pageSize": max_results,
        })
        if d.status_code == 200:
            contacts += [_parse_person(p) for p in (d.json().get("people") or [])]

    return _dedupe_sort(contacts)


async def search_contacts(user_id: str, query: str) -> list[dict]:
    """Search the user's contacts by name/email/phone."""
    if not GOOGLE_CONFIGURED:
        return _MOCK
    if not query.strip():
        return []
    headers = await get_google_headers(user_id)
    r = await google_request("GET", f"{PEOPLE}/people:searchContacts", headers=headers, params={
        "query": query, "readMask": PERSON_FIELDS, "pageSize": 25,
    })
    if r.status_code != 200:
        log.warning("people search failed for %s: %s", user_id, r.status_code)
        return []
    results = r.json().get("results") or []
    return _dedupe_sort([_parse_person(item.get("person", {})) for item in results])


async def get_contact_by_email(user_id: str, email: str) -> dict | None:
    """Find a single contact by email (used for agent enrichment)."""
    if not email:
        return None
    for c in await search_contacts(user_id, email):
        if (c.get("email") or "").lower() == email.lower():
            return c
    return None
