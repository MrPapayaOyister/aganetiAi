"""provider OAuth connections — move the system of record off the JSON file

Revision ID: d4e91a3b7c62
Revises: c1a7f0b2e9d4
Create Date: 2026-08-07

Hand-written, matching the existing convention in this directory (autogenerate
emits unrelated destructive operations against the drifted model tree).

Purely additive: one new table. Nothing running references it until
provider_tokens is pointed at the Postgres store, so this applies with no restart
and no downtime, and `downgrade` drops only what `upgrade` created.

Why this exists
---------------
Provider OAuth tokens were stored in `data_vault/provider_connections.json`, a
single-host file. Two backend replicas therefore disagree about who is connected,
and a lost disk loses every user's Microsoft and Google authorisation.

The intended store was Supabase (see backend/db/provider_connections.sql), but
that path is dead in this deployment: the service key is the newer `sb_secret_…`
format and PostgREST answers 401 "Invalid API key". Fixing that means minting a
new key in the Supabase dashboard — outside the backend's control. The app's own
Postgres is already migrated, pooled and backed up, so it becomes the system of
record and the JSON file drops to emergency fallback.

Differences from the Supabase DDL, and why
------------------------------------------
* `user_id` is TEXT, not UUID. Internal aliases ("user_1", "test") are real ids in
  this system and would be rejected by a UUID column — the JSON store holds two
  such rows today.
* No RLS policies. `auth.uid()` and the `service_role` grant are Supabase
  constructs; this database is reached only by the backend's own pooled
  connection, so row-level security here would protect nothing and fail to apply.
* `updated_at` is maintained by the application's UPDATE statements rather than a
  trigger, keeping the write path visible in one place.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4e91a3b7c62"
down_revision = "c1a7f0b2e9d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        # TEXT, not UUID — see module docstring.
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_email", sa.Text(), nullable=True),
        # Ciphertext, never plaintext: token_crypto encrypts one layer above.
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("raw_profile", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("provider IN ('google', 'microsoft')",
                           name="provider_connections_provider_check"),
        # The ON CONFLICT target in _token_pg_store.upsert. Without it two
        # concurrent OAuth callbacks for one user race into duplicate rows.
        sa.UniqueConstraint("user_id", "provider", name="provider_connections_user_provider_key"),
    )
    op.create_index("provider_connections_user_idx", "provider_connections",
                    ["user_id", "provider"])
    # pgcrypto backs gen_random_uuid(). Postgres 13+ has it built in, but the
    # server_default above is evaluated by the database, so make it explicit.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    op.drop_index("provider_connections_user_idx", table_name="provider_connections")
    op.drop_table("provider_connections")
