from __future__ import annotations

import json
import subprocess

import src.publish_analytics as publish_analytics
from src.storage import Storage
from tests.conftest import make_settings


def _proc(returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class FakeGit:
    """Records calls and returns scripted results per git subcommand."""

    def __init__(self, script: dict):
        self.calls = []
        self._script = dict(script)
        self._push_call_count = 0

    def __call__(self, *args):
        self.calls.append(args)
        subcommand = args[0]
        if subcommand == "push":
            self._push_call_count += 1
            pushes = self._script.get("push")
            if isinstance(pushes, list):
                return pushes[min(self._push_call_count, len(pushes)) - 1]
            return pushes or _proc(0)
        return self._script.get(subcommand, _proc(0))


def _settings_with_settled_market(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = Storage(db_path)
    storage._conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result) VALUES (?, ?)", ("T1", "yes")
    )
    storage._conn.commit()
    storage.close()
    return make_settings(db_path=db_path)


def test_publish_once_writes_analytics_json(monkeypatch, tmp_path):
    output_path = tmp_path / "analytics.json"
    monkeypatch.setattr(publish_analytics, "OUTPUT_PATH", output_path)
    fake_git = FakeGit({"diff": _proc(1), "commit": _proc(0), "push": _proc(0)})
    monkeypatch.setattr(publish_analytics, "_run_git", fake_git)

    settings = _settings_with_settled_market(tmp_path)
    pushed = publish_analytics.publish_once(settings)

    assert pushed is True
    data = json.loads(output_path.read_text())
    assert data["pattern_log"]["total_settled"] == 1


def test_no_commit_when_nothing_changed(monkeypatch, tmp_path):
    output_path = tmp_path / "analytics.json"
    monkeypatch.setattr(publish_analytics, "OUTPUT_PATH", output_path)
    fake_git = FakeGit({"diff": _proc(0)})  # 0 = no diff, per `git diff --quiet` semantics
    monkeypatch.setattr(publish_analytics, "_run_git", fake_git)

    settings = _settings_with_settled_market(tmp_path)
    pushed = publish_analytics.publish_once(settings)

    assert pushed is False
    assert not any(call[0] == "commit" for call in fake_git.calls)


def test_commit_failure_does_not_push(monkeypatch, tmp_path):
    output_path = tmp_path / "analytics.json"
    monkeypatch.setattr(publish_analytics, "OUTPUT_PATH", output_path)
    fake_git = FakeGit({"diff": _proc(1), "commit": _proc(1, "commit failed")})
    monkeypatch.setattr(publish_analytics, "_run_git", fake_git)

    settings = _settings_with_settled_market(tmp_path)
    pushed = publish_analytics.publish_once(settings)

    assert pushed is False
    assert not any(call[0] == "push" for call in fake_git.calls)


def test_push_retries_with_rebase_then_succeeds(monkeypatch, tmp_path):
    output_path = tmp_path / "analytics.json"
    monkeypatch.setattr(publish_analytics, "OUTPUT_PATH", output_path)
    monkeypatch.setattr(publish_analytics.time, "sleep", lambda *_: None)
    fake_git = FakeGit(
        {
            "diff": _proc(1),
            "commit": _proc(0),
            "push": [_proc(1, "non-fast-forward"), _proc(0)],
        }
    )
    monkeypatch.setattr(publish_analytics, "_run_git", fake_git)

    settings = _settings_with_settled_market(tmp_path)
    pushed = publish_analytics.publish_once(settings)

    assert pushed is True
    assert [c[0] for c in fake_git.calls].count("push") == 2
    assert any(call[0] == "pull" for call in fake_git.calls)


def test_push_gives_up_after_max_attempts(monkeypatch, tmp_path):
    output_path = tmp_path / "analytics.json"
    monkeypatch.setattr(publish_analytics, "OUTPUT_PATH", output_path)
    monkeypatch.setattr(publish_analytics.time, "sleep", lambda *_: None)
    fake_git = FakeGit({"diff": _proc(1), "commit": _proc(0), "push": _proc(1, "still failing")})
    monkeypatch.setattr(publish_analytics, "_run_git", fake_git)

    settings = _settings_with_settled_market(tmp_path)
    pushed = publish_analytics.publish_once(settings)

    assert pushed is False
    assert [c[0] for c in fake_git.calls].count("push") == publish_analytics.MAX_PUSH_ATTEMPTS
