from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.helpers import alembic_config


def test_organized_migration_preserves_unknown_state(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    cfg = alembic_config(db_path)
    previous, target = "a9c01801c865", "0b4c0aba7e89"
    command.upgrade(cfg, previous)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_files (path, library_id, status, has_subtitle_in_name, has_external_subtitle, "
                "created_at, updated_at) VALUES ('Z:/offline/ABC-123.mp4', 1, 'SCRAPED', 0, 0, "
                "'2026-01-01', '2026-01-01')"
            )
        )
    command.upgrade(cfg, target)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT path, organized FROM media_files")).one()
        assert row.path == "Z:/offline/ABC-123.mp4" and row.organized == 0
        assert (
            next(c for c in inspect(conn).get_columns("media_files") if c["name"] == "organized")["nullable"] is False
        )
    command.downgrade(cfg, previous)
    command.upgrade(cfg, target)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT organized FROM media_files")).scalar_one() == 0
    engine.dispose()
