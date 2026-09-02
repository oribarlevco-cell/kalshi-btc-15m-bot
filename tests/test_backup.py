from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from src.backup import backup_database, export_csv


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.execute("CREATE TABLE settled_outcomes (ticker TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.execute("CREATE TABLE market_lifecycle (ticker TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE orderbook_snapshots (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.executemany("INSERT INTO snapshots (ticker) VALUES (?)", [("A",), ("B",), ("C",)])
    conn.commit()
    conn.close()


def test_backup_database_produces_queryable_copy(tmp_path):
    db_path = str(tmp_path / "source.db")
    backup_dir = str(tmp_path / "backups")
    _make_db(db_path)

    dest = backup_database(db_path, backup_dir)

    assert dest.exists()
    conn = sqlite3.connect(str(dest))
    count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    conn.close()
    assert count == 3


def test_backup_database_prunes_to_keep_count(tmp_path):
    db_path = str(tmp_path / "source.db")
    backup_dir = str(tmp_path / "backups")
    _make_db(db_path)

    for _ in range(4):
        backup_database(db_path, backup_dir, keep=3)
        time.sleep(1.05)  # timestamps have 1s resolution; force distinct filenames

    remaining = sorted(Path(backup_dir).glob("kalshi_btc15m_*.db"))
    assert len(remaining) == 3


def test_backup_database_keep_zero_or_negative_prunes_nothing(tmp_path):
    db_path = str(tmp_path / "source.db")
    backup_dir = str(tmp_path / "backups")
    _make_db(db_path)

    backup_database(db_path, backup_dir, keep=0)
    time.sleep(1.05)  # timestamps have 1s resolution; force distinct filenames
    backup_database(db_path, backup_dir, keep=0)

    remaining = list(Path(backup_dir).glob("kalshi_btc15m_*.db"))
    assert len(remaining) == 2


def test_export_csv_writes_nonempty_tables_only(tmp_path):
    db_path = str(tmp_path / "source.db")
    backup_dir = str(tmp_path / "backups")
    _make_db(db_path)

    written = export_csv(db_path, backup_dir)

    assert len(written) == 1
    assert written[0].name.startswith("snapshots_")
    assert written[0].suffix == ".csv"

    content = written[0].read_text().strip().splitlines()
    assert content[0] == "id,ticker"
    assert len(content) == 4  # header + 3 rows
