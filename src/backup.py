from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config.settings import load_settings

TABLES = ["snapshots", "settled_outcomes", "predictions", "orders", "market_lifecycle", "orderbook_snapshots"]


def backup_database(db_path: str, backup_dir: str, keep: int = 10) -> Path:
    """Copy the live SQLite DB using sqlite3's online backup API (safe
    against a concurrently-open/writing connection, unlike a raw file copy),
    then prune down to the `keep` most recent backups."""
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(backup_dir) / f"kalshi_btc15m_{timestamp}.db"

    source_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(str(dest))
    try:
        source_conn.backup(dest_conn)
    finally:
        source_conn.close()
        dest_conn.close()

    _prune_old_backups(backup_dir, "kalshi_btc15m_*.db", keep)
    return dest


def export_csv(db_path: str, backup_dir: str) -> list[Path]:
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    written: list[Path] = []
    try:
        for table in TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            dest = Path(backup_dir) / f"{table}_{timestamp}.csv"
            with open(dest, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(rows[0].keys())
                writer.writerows(tuple(row) for row in rows)
            written.append(dest)
    finally:
        conn.close()
    return written


def _prune_old_backups(backup_dir: str, pattern: str, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(Path(backup_dir).glob(pattern), key=lambda p: p.name)
    for old in files[:-keep]:
        old.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the local Kalshi bot SQLite database")
    parser.add_argument("--csv", action="store_true", help="Also export each table to CSV")
    parser.add_argument("--keep", type=int, default=10, help="Number of .db backups to retain")
    args = parser.parse_args()

    settings = load_settings()
    dest = backup_database(settings.db_path, settings.backup_dir, keep=args.keep)
    print(f"Backed up to {dest}")
    if args.csv:
        for path in export_csv(settings.db_path, settings.backup_dir):
            print(f"Exported {path}")


if __name__ == "__main__":
    main()
