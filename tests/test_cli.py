"""JSON CLI behavior with synthetic rollout data only."""

from __future__ import annotations

import json

from codex_context_meter.cli import main


THREAD_ID = "33333333-3333-3333-3333-333333333333"
ENV_THREAD_ID = "44444444-4444-4444-4444-444444444444"


def write_rollout(tmp_path, thread_id=THREAD_ID):
    path = tmp_path / f"rollout-synthetic-{thread_id}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": thread_id}},
        {
            "timestamp": "2026-01-02T12:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    "model_context_window": 100,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def output_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_cli_uses_environment_thread_id_only_when_option_is_absent(tmp_path, monkeypatch, capsys):
    path = write_rollout(tmp_path, ENV_THREAD_ID)
    monkeypatch.setenv("CODEX_THREAD_ID", ENV_THREAD_ID)

    code = main(["--session-file", str(path)])

    data = output_json(capsys)
    assert code == 0
    assert data["thread_id"] == ENV_THREAD_ID
    assert data["remaining_estimate"]["tokens"] == 85


def test_cli_explicit_thread_id_wins_and_empty_explicit_value_does_not_fall_back(tmp_path, monkeypatch, capsys):
    path = write_rollout(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", ENV_THREAD_ID)

    code = main(["--thread-id", THREAD_ID, "--session-file", str(path), "--pretty"])

    data = output_json(capsys)
    assert code == 0
    assert data["thread_id"] == THREAD_ID

    code = main(["--thread-id", "", "--session-file", str(path)])
    data = output_json(capsys)
    assert code == 2
    assert data["error"]["code"] == "thread_id_required"


def test_cli_always_writes_structured_json_for_domain_errors(capsys):
    code = main(["--thread-id", "path-traversal"])

    data = output_json(capsys)
    assert code == 2
    assert data == {
        "schema_version": 1,
        "status": "unavailable",
        "error": {"code": "invalid_thread_id", "message": "Provide the exact Codex thread UUID."},
    }


def test_cli_reports_invalid_domain_options_as_json(tmp_path, capsys):
    path = write_rollout(tmp_path)

    code = main(["--thread-id", THREAD_ID, "--session-file", str(path), "--compact-threshold", "0"])

    data = output_json(capsys)
    assert code == 2
    assert data["error"]["code"] == "invalid_compact_limit"
