from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def pool(url: str) -> ConnectionPool:
    return ConnectionPool(
        url,
        min_size=2,
        max_size=24,
        timeout=8,
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": 5,
            "options": "-c lock_timeout=5000 -c statement_timeout=15000",
        },
        open=True,
    )


def migrate(db: ConnectionPool) -> None:
    """Versioned, checksummed migrations, serialized across API/worker startup."""
    import hashlib

    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(72010421)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY, checksum text NOT NULL)"
        )
        for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            content = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode()).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=%s", (path.name,)
            ).fetchone()
            if row:
                if row["checksum"] != checksum:
                    raise RuntimeError(f"Migration changed after applying: {path.name}")
                continue
            conn.execute(content)
            conn.execute("INSERT INTO schema_migrations VALUES (%s,%s)", (path.name, checksum))
