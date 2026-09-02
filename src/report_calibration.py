from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass

from config.settings import load_settings


@dataclass
class Bucket:
    lo: float
    hi: float
    count: int
    mean_predicted: float
    actual_yes_rate: float


def fetch_rows(db_path: str, which: str) -> list[tuple[float, int]]:
    """Return (predicted_probability_yes, actual_yes_as_1_or_0) pairs for
    every settled market that has a usable prediction of the requested kind."""
    prob_col = "initial_probability_yes" if which == "initial" else "last_probability_yes"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {prob_col}, actual_result FROM market_lifecycle "  # noqa: S608 (prob_col is one of two literals above)
            f"WHERE {prob_col} IS NOT NULL AND actual_result IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [(p, 1 if r == "yes" else 0) for p, r in rows]


def bucketize(rows: list[tuple[float, int]], n_bins: int) -> list[Bucket]:
    buckets = []
    width = 1.0 / n_bins
    for i in range(n_bins):
        lo, hi = i * width, (i + 1) * width
        in_bucket = [(p, y) for p, y in rows if (lo <= p < hi) or (hi >= 1.0 and p == 1.0)]
        if not in_bucket:
            continue
        mean_p = sum(p for p, _ in in_bucket) / len(in_bucket)
        yes_rate = sum(y for _, y in in_bucket) / len(in_bucket)
        buckets.append(Bucket(lo, hi, len(in_bucket), mean_p, yes_rate))
    return buckets


def brier_score(rows: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in rows) / len(rows)


def directional_accuracy(rows: list[tuple[float, int]]) -> float:
    correct = sum(1 for p, y in rows if (p >= 0.5) == (y == 1))
    return correct / len(rows)


def print_report(rows: list[tuple[float, int]], n_bins: int) -> None:
    if not rows:
        print("No settled markets with predictions yet.")
        return

    print(f"{'range':>12}  {'n':>5}  {'mean pred':>10}  {'actual yes%':>12}")
    for b in bucketize(rows, n_bins):
        range_label = f"{b.lo * 100:4.0f}-{b.hi * 100:3.0f}%"
        print(f"{range_label}  {b.count:5d}  {b.mean_predicted * 100:9.1f}%  {b.actual_yes_rate * 100:11.1f}%")

    print()
    print(f"n = {len(rows)}")
    print(f"Brier score: {brier_score(rows):.4f}  (0 = perfect, 0.25 = no better than a coinflip)")
    print(f"Directional accuracy (>=50% called YES): {directional_accuracy(rows) * 100:.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare model predictions to actual settled Kalshi outcomes")
    parser.add_argument(
        "--which", choices=["initial", "last"], default="last", help="Use each market's earliest or latest prediction"
    )
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()

    settings = load_settings()
    db_path = args.db_path or settings.db_path
    rows = fetch_rows(db_path, args.which)
    print_report(rows, args.bins)


if __name__ == "__main__":
    main()
