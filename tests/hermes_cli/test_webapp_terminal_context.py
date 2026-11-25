"""Webapp handoff inventories this host only for an authenticated profile."""
from pathlib import Path

from fastapi.testclient import TestClient


def test_terminal_context_auth_profile_and_host_shells(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_platform.resolver import locate_command

    home = tmp_path / "hermes-root"
    profile_home = home / "profiles" / "work"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(web_server.app.state, "ui_surface", "webapp", raising=False)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "context-token")
    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    path = "/api/webapp/terminal-context"
    assert client.get(path).status_code == 401
    headers = {"X-Hermes-Session-Token": "context-token"}
    for profile, expected in (("default", home), ("work", profile_home)):
        response = client.get(path, params={"profile": profile}, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "hermes_home": str(expected),
            "shells": {shell: bool(locate_command(shell).command) for shell in ("bash", "zsh")},
        }
    assert client.get(path, params={"profile": "missing"}, headers=headers).status_code == 404
