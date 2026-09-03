from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from config.settings import Settings, load_settings
from src.analytics import build_analytics_payload

logger = logging.getLogger("kalshi_bot")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "analytics.json"

MAX_PUSH_ATTEMPTS = 3


def _run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def _commit_and_push() -> bool:
    """Commit docs/analytics.json if it changed and push, retrying with a
    rebase if origin advanced in the meantime (e.g. the GH Actions workflow
    committed docs/data.json around the same time -- a different file, but
    still a non-fast-forward on the branch). Returns True if a commit was
    pushed, False if there was nothing to commit."""
    _run_git("add", "docs/analytics.json")

    diff = _run_git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return False  # nothing changed

    commit = _run_git("commit", "-m", "Update dashboard analytics [skip ci]")
    if commit.returncode != 0:
        logger.error("git commit failed: %s", commit.stderr.strip())
        return False

    for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
        push = _run_git("push")
        if push.returncode == 0:
            return True
        logger.warning("git push failed (attempt %d/%d): %s", attempt, MAX_PUSH_ATTEMPTS, push.stderr.strip())
        _run_git("pull", "--rebase")
        time.sleep(2)

    logger.error("Giving up on pushing docs/analytics.json after %d attempts", MAX_PUSH_ATTEMPTS)
    return False


def publish_once(settings: Settings) -> bool:
    """Build docs/analytics.json from the local DB and publish it. Returns
    True if a new commit was pushed."""
    payload = build_analytics_payload(settings)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2))
    pushed = _commit_and_push()
    if pushed:
        logger.info("Published docs/analytics.json (%d settled markets)", payload["pattern_log"]["total_settled"])
    return pushed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    publish_once(settings)


if __name__ == "__main__":
    main()
