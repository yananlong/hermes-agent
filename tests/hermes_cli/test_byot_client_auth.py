"""The BYOT client must use the dashboard's configured credential exactly."""
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_cli.byot_bridge import run_byot_bridge


def _capture_connection(monkeypatch):
    urls = []

    def connect(url, **kwargs):
        urls.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr("hermes_cli.byot_bridge.connect", connect)
    return urls


@pytest.mark.parametrize("explicit", [False, True])
def test_bridge_resolves_file_token_and_encodes_query(tmp_path, monkeypatch, capsys, explicit):
    secret = "a+b&c=/?#"
    token_file = tmp_path / "dashboard-token"
    token_file.write_text(f"\ufeff{secret}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", f"file:{token_file}")
    urls = _capture_connection(monkeypatch)
    token = "explicit+&secret" if explicit else ""
    assert run_byot_bridge(url="https://dashboard.example", token=token) == 2
    assert parse_qs(urlsplit(urls[0]).query)["token"] == [token or secret]
    assert secret not in capsys.readouterr().err


def test_bridge_does_not_generate_a_token_when_unconfigured(monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    urls = _capture_connection(monkeypatch)
    assert run_byot_bridge() == 2
    assert urls == []


def test_bridge_rejects_missing_token_file_without_dialing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", f"file:{tmp_path / 'missing'}")
    urls = _capture_connection(monkeypatch)
    with pytest.raises(SystemExit, match="unreadable token file"):
        run_byot_bridge()
    assert urls == []


def test_spawned_terminal_does_not_inherit_dashboard_credential(tmp_path, monkeypatch):
    from hermes_cli import byot_bridge

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "super-secret")
    captured = {}

    class Socket:
        def send(self, data):
            pass
        def recv(self, timeout):
            return '{"type":"registered","id":"bridge-1"}'
        def close(self):
            pass

    def spawn(command, cols, rows, env):
        captured.update(env)
        return object()

    monkeypatch.setattr(byot_bridge, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(byot_bridge, "_spawn_pty", spawn)
    monkeypatch.setattr(byot_bridge._Relay, "run", lambda self: 0)
    assert run_byot_bridge(command=["/bin/sh"]) == 0
    assert "HERMES_DASHBOARD_SESSION_TOKEN" not in captured
    assert captured["HERMES_HOME"] == str(tmp_path)
