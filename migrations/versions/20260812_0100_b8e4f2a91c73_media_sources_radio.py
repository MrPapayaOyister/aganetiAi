"""media_sources: carry codec/bitrate so a saved radio station renders like a live one

Revision ID: b8e4f2a91c73
Revises: a7f3c9d21e88
Create Date: 2026-08-12

Hand-written, matching the convention in this directory.

Purely additive: two nullable columns. Nothing reads them until the radio library
lands, so this applies with no restart and no downtime, and `downgrade` drops only
what `upgrade` created.

Why these two and not a `radio_stations` table
----------------------------------------------
A saved station differs from a saved TV channel in exactly one respect — what
plays it — and shares everything else: name, url, category, per-user ownership,
enable/disable, the validation timestamp. A second table would duplicate the
add/remove/list surface and the ownership rules to gain one discriminator that
`kind` already provides.

What it does NOT share is the validation path, and that is the real schema
question. `kind` now decides:

  tv    -> HLS: must return #EXTM3U, and must permit an opaque origin, because
           hls.js fetches the manifest and segments by XHR and a sandboxed frame
           sends `Origin: null`.
  radio -> a continuous audio stream: https, not .m3u8, audio content-type, and
           no finite Content-Length. NO CORS requirement — a media element loads
           cross-origin audio without any Access-Control-Allow-Origin header,
           which was verified in a browser before this was written. Requiring it
           would reject most working stations.

codec and bitrate exist because the radio card shows them as badges ("128kbps
MP3"). Radio Browser supplies both, and without somewhere to put them a saved
station would render more poorly than the live result it was saved from — the
one thing a library must not do.
"""
from alembic import op
import sqlalchemy as sa

revision = "b8e4f2a91c73"
down_revision = "a7f3c9d21e88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: TV rows have neither, and a station saved before the directory
    # knew its codec should not be blocked from being saved at all.
    op.add_column("media_sources", sa.Column("codec", sa.Text(), nullable=True))
    op.add_column("media_sources", sa.Column("bitrate", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_sources", "bitrate")
    op.drop_column("media_sources", "codec")
