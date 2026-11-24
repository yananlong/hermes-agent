"""A large native history must arrive before the app-server request deadline."""

import stat
import sys

import pytest

from agent.transports.codex_app_server import CodexAppServerClient


@pytest.mark.platforms("posix")
def test_large_history_reply_does_not_block_subsequent_requests(tmp_path):
    """Codex can return a multi-megabyte JSON line for a long conversation."""
    size = 12 * 1024 * 1024
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request:\n"
        "        continue\n"
        f"    result = {{'history': 'x' * {size}}} if request['method'] == 'thread/turns/list' else {{'ok': True}}\n"
        "    print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    with CodexAppServerClient(codex_bin=str(executable), codex_home=str(tmp_path)) as client:
        client.initialize(timeout=6)
        result = client.request("thread/turns/list", {"threadId": "synthetic"}, timeout=6)
        assert len(result["history"]) == size
        assert client.request("small", timeout=6) == {"ok": True}
