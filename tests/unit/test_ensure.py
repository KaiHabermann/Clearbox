"""`clearbox ensure`: leave a running Clearbox alone, refuse a port held by something else, and
otherwise start a background server with the same options and wait until it answers."""

from __future__ import annotations

import http.server
import socket
import subprocess
import threading

import clearbox.__main__ as cli


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


def test_running_clearbox_is_left_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "probe", lambda *a, **k: "clearbox")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert cli.ensure(["--port", "9123"], str(tmp_path), None) == 0
    assert "Clearbox is running: http://127.0.0.1:9123/" in capsys.readouterr().out


def test_port_held_by_something_else(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "probe", lambda *a, **k: "other")
    assert cli.ensure(["--port", "9123"], str(tmp_path), None) == 1
    assert "not Clearbox" in capsys.readouterr().err


def test_starts_a_server_with_the_same_options(tmp_path, monkeypatch, capsys):
    answers = iter([None, None, "clearbox"])
    monkeypatch.setattr(cli, "probe", lambda *a, **k: next(answers))
    started = {}

    def popen(cmd, **kw):
        started.update(cmd=cmd, kw=kw)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    argv = ["--port", "9123", "--mock"]
    assert cli.ensure(argv, str(tmp_path), None) == 0
    assert started["cmd"][1:] == ["-m", "clearbox", *argv]
    assert started["kw"]["cwd"] == str(tmp_path) and (tmp_path / "server.log").exists()
    assert "Started Clearbox in the background" in capsys.readouterr().out


def test_server_that_exits_at_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "probe", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: FakeProc(code=2))
    assert cli.ensure(["--port", "9123"], str(tmp_path), None) == 1
    assert "exited at once" in capsys.readouterr().err


def test_probe_tells_clearbox_from_other_servers():
    port = free_port()
    assert cli.probe("127.0.0.1", port) is None  # nothing listening
    other = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert cli.probe("127.0.0.1", other.server_address[1]) == "other"  # answers 501, not Clearbox
    finally:
        other.shutdown()
        other.server_close()


def test_real_background_server(tmp_path, monkeypatch):
    # one real start: the detached server answers /api/health, and a second ensure only finds it
    procs = []
    real_popen = subprocess.Popen

    def popen(*a, **kw):
        procs.append(real_popen(*a, **kw))
        return procs[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    port = free_port()
    argv = ["--port", str(port), "--mock", "--max-concurrency", "0",
            "--db", str(tmp_path / "t.db"), "--workdir", str(tmp_path / "ws")]
    try:
        assert cli.ensure(argv, str(tmp_path), None, wait=30) == 0
        assert cli.probe("127.0.0.1", port) == "clearbox"
        assert cli.ensure(argv, str(tmp_path), None) == 0 and len(procs) == 1
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=10)
