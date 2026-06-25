"""
Supabase Python client singletons.

- anon_client: for user-scoped operations (respects RLS, needs user JWT)
- admin_client: for backend operations (service role, bypasses RLS)

Neither is imported at module load — call get_supabase_admin() / get_supabase_anon()
on first use so startup errors are visible early.
"""
import os
from functools import lru_cache

from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


@lru_cache(maxsize=1)
def get_supabase_admin() -> Client:
    """Service-role client — bypasses RLS. Use only for trusted backend operations."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


@lru_cache(maxsize=1)
def get_supabase_anon() -> Client:
    """Anon-key client — respects RLS. Use when acting on behalf of a user."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
