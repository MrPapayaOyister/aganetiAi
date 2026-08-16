"""move the Telegram chat mapping out of .env and onto users

Revision ID: a7f3c9d21e88
Revises: d4e91a3b7c62
Create Date: 2026-08-11

Hand-written, matching the convention in this directory.

Purely additive: one nullable column and a partial unique index. Nothing reads it
until backend/services/user_directory.py is wired in, so it applies with no
restart and no downtime, and `downgrade` drops only what `upgrade` created.

Why this exists
---------------
Telegram identity lived in `config/users.py`, a two-entry dict built from
USER_1_TELEGRAM_ID / USER_2_TELEGRAM_ID. Onboarding a user meant editing .env and
restarting the process, so the registry could not grow past the handful of people
someone was willing to hand-maintain — and it had already drifted out of step with
reality: seven users exist in `users`, two in the registry.

Telegram chat ids are per-user identity, exactly like `supabase_uid` beside them,
so they belong on the row rather than in deployment config.

Why a PARTIAL unique index
--------------------------
The mapping is the authorisation check on the Telegram side: an inbound chat id is
resolved to a user, and an unknown id is refused. Two users sharing a chat id would
make that resolution ambiguous and could route one person's assistant — their mail,
calendar and tasks — to the other's chat.

Postgres treats NULLs as distinct in a plain unique index, which is what we want
(most users never link Telegram, and they must not collide with each other), but
`WHERE telegram_chat_id IS NOT NULL` states that intent explicitly rather than
leaving it to a subtlety of NULL semantics.

BIGINT, not INTEGER: Telegram ids for channels and supergroups already exceed
2^31, and a negative id is legitimate for group chats.
"""
from alembic import op
import sqlalchemy as sa

revision = "a7f3c9d21e88"
down_revision = "d4e91a3b7c62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "users_telegram_chat_id_key",
        "users",
        ["telegram_chat_id"],
        unique=True,
        postgresql_where=sa.text("telegram_chat_id IS NOT NULL"),
    )
    # Carry over whatever the retiring registry held, so a user who has Telegram
    # linked today keeps it across the cutover. Matching is by supabase_uid — the
    # only field the two schemes share. Written as a no-op when the env vars are
    # absent, which is the normal case on a fresh deployment.
    conn = op.get_bind()
    import os

    for slot in ("1", "2"):
        sub = (os.getenv(f"USER_{slot}_SUPABASE_UID") or "").strip()
        raw = (os.getenv(f"USER_{slot}_TELEGRAM_ID") or "").strip()
        if not sub or not raw:
            continue
        try:
            chat_id = int(raw)
        except ValueError:
            continue
        if chat_id == 0:
            continue
        conn.execute(
            sa.text(
                "UPDATE users SET telegram_chat_id = :cid "
                "WHERE supabase_uid = :sub AND telegram_chat_id IS NULL"
            ),
            {"cid": chat_id, "sub": sub},
        )


def downgrade() -> None:
    op.drop_index("users_telegram_chat_id_key", table_name="users")
    op.drop_column("users", "telegram_chat_id")
