import pytest

from browser_harness import admin


class FakeSocket:
    def __init__(self, response=b'{"target_id":"target-1","session_id":"session-1","page":null}\n'):
        self.response = response
        self.closed = False
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        out, self.response = self.response, b""
        return out

    def close(self):
        self.closed = True


def test_local_chrome_mode_is_false_when_env_provides_remote_cdp():
    assert not admin._is_local_chrome_mode({"BU_CDP_WS": "ws://example.test/devtools/browser/1"})


def test_local_chrome_mode_is_false_when_process_env_provides_remote_cdp(monkeypatch):
    monkeypatch.setenv("BU_CDP_WS", "ws://example.test/devtools/browser/1")

    assert not admin._is_local_chrome_mode()


def test_handshake_timeout_needs_chrome_remote_debugging_prompt():
    msg = "CDP WS handshake failed: timed out during opening handshake"

    assert admin._needs_chrome_remote_debugging_prompt(msg)


def test_handshake_403_needs_chrome_remote_debugging_prompt():
    msg = "CDP WS handshake failed: server rejected WebSocket connection: HTTP 403"

    assert admin._needs_chrome_remote_debugging_prompt(msg)


def test_stale_websocket_does_not_open_chrome_inspect():
    msg = "no close frame received or sent"

    assert not admin._needs_chrome_remote_debugging_prompt(msg)


def test_daemon_endpoint_names_discovers_valid_socket_names(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.ipc, "IS_WINDOWS", False)
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR", None)  # shared-tmpdir mode
    monkeypatch.setattr(admin.ipc, "_RUNTIME", tmp_path)
    (tmp_path / "bu-default.sock").touch()
    (tmp_path / "bu-remote_1.sock").touch()
    (tmp_path / "bu-invalid.name.sock").touch()
    (tmp_path / "not-bu-default.sock").touch()

    assert admin._daemon_endpoint_names() == ["default", "remote_1"]


def test_daemon_endpoint_names_with_bh_runtime_dir_returns_local_name_when_sock_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.ipc, "IS_WINDOWS", False)
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR_SHARED", False)
    monkeypatch.setattr(admin.ipc, "_RUNTIME", tmp_path)
    monkeypatch.setattr(admin, "NAME", "session-xyz")
    (tmp_path / "bu.sock").touch()

    assert admin._daemon_endpoint_names() == ["session-xyz"]


def test_daemon_endpoint_names_with_bh_runtime_dir_returns_empty_when_sock_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.ipc, "IS_WINDOWS", False)
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR_SHARED", False)
    monkeypatch.setattr(admin.ipc, "_RUNTIME", tmp_path)
    monkeypatch.setattr(admin, "NAME", "session-xyz")

    assert admin._daemon_endpoint_names() == []


def test_daemon_endpoint_names_with_shared_bh_runtime_dir_discovers_named_sockets(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.ipc, "IS_WINDOWS", False)
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(admin.ipc, "BH_RUNTIME_DIR_SHARED", True)
    monkeypatch.setattr(admin.ipc, "_RUNTIME", tmp_path)
    (tmp_path / "bu-default.sock").touch()
    (tmp_path / "bu-work.sock").touch()
    (tmp_path / "bu-invalid.name.sock").touch()
    (tmp_path / "bu.sock").touch()  # stale isolated-runtime endpoint

    assert admin._daemon_endpoint_names() == ["default", "work"]


def test_active_browser_connections_counts_only_healthy_daemons(monkeypatch):
    monkeypatch.setattr(admin, "_daemon_endpoint_names", lambda: ["default", "stale", "remote"])

    def fake_connect(name, timeout=1.0):
        if name == "stale":
            raise ConnectionRefusedError()
        if name == "remote":
            return FakeSocket(b'{"error":"no close frame received or sent"}\n'), None
        return FakeSocket(), None

    monkeypatch.setattr(admin.ipc, "connect", fake_connect)

    assert admin.active_browser_connections() == 1


def test_active_browser_connections_skips_daemons_reporting_cdp_disconnected(monkeypatch):
    monkeypatch.setattr(admin, "_daemon_endpoint_names", lambda: ["default", "stale"])

    def fake_connect(name, timeout=1.0):
        if name == "stale":
            return FakeSocket(b'{"error":"cdp_disconnected"}\n'), None
        return FakeSocket(), None

    monkeypatch.setattr(admin.ipc, "connect", fake_connect)

    assert admin.active_browser_connections() == 1


def test_browser_connections_returns_attached_page(monkeypatch):
    monkeypatch.setattr(admin, "_daemon_endpoint_names", lambda: ["default"])
    response = (
        b'{"target_id":"target-1","session_id":"session-1",'
        b'"page":{"targetId":"target-1","title":"Cat - Wikipedia","url":"https://en.wikipedia.org/wiki/Cat"}}\n'
    )
    monkeypatch.setattr(admin.ipc, "connect", lambda name, timeout=1.0: (FakeSocket(response), None))

    assert admin.browser_connections() == [
        {
            "name": "default",
            "page": {"title": "Cat - Wikipedia", "url": "https://en.wikipedia.org/wiki/Cat"},
        }
    ]


def test_chrome_running_detects_helium_on_linux(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *args, **kwargs: "systemd\nhelium\nxdg-desktop-portal\n",
    )

    assert admin._chrome_running()


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/snap/chromium/1234/usr/lib/chromium-browser/chromium-browser", True),
        ("/SNAP/foo", True),
        ("/usr/bin/google-chrome-stable", False),
        ("", False),
    ],
)
def test_is_snap_browser(path, expected):
    assert admin._is_snap_browser(path) == expected


def test_doctor_probe_preserves_snap_bin_env_symlink(monkeypatch, tmp_path):
    target = tmp_path / "usr" / "bin" / "snap"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    snap_bin = tmp_path / "snap" / "bin"
    snap_bin.mkdir(parents=True)
    chromium = snap_bin / "chromium"
    chromium.symlink_to(target)

    monkeypatch.setenv("BH_CHROME_PATH", str(chromium))
    monkeypatch.delenv("CHROME_PATH", raising=False)

    name, path = admin._doctor_probe_chrome_binary_for_snap()

    assert name == "chromium"
    assert path == str(chromium)
    assert admin._is_snap_browser(path)


def test_doctor_probe_preserves_snap_bin_path_symlink(monkeypatch, tmp_path):
    target = tmp_path / "usr" / "bin" / "snap"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    snap_bin = tmp_path / "snap" / "bin"
    snap_bin.mkdir(parents=True)
    chromium = snap_bin / "chromium"
    chromium.symlink_to(target)

    monkeypatch.delenv("BH_CHROME_PATH", raising=False)
    monkeypatch.delenv("CHROME_PATH", raising=False)

    def fake_which(cmd):
        return str(chromium) if cmd == "chromium" else None

    monkeypatch.setattr("shutil.which", fake_which)

    name, path = admin._doctor_probe_chrome_binary_for_snap()

    assert name == "chromium"
    assert path == str(chromium)
    assert admin._is_snap_browser(path)


def test_run_doctor_prints_snap_detect_on_linux_when_probe_is_snap(monkeypatch, capsys):
    monkeypatch.setattr(admin, "_version", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_install_mode", lambda: "git")
    monkeypatch.setattr(admin, "_chrome_running", lambda: False)
    monkeypatch.setattr(admin, "daemon_alive", lambda: False)
    monkeypatch.setattr(admin, "browser_connections", lambda: [])
    monkeypatch.setattr(admin, "_latest_release_tag", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_doctor_probe_chrome_binary_for_snap", lambda: ("chromium", "/snap/chromium/1/usr/bin/chromium"))
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)

    assert admin.run_doctor() == 1

    out = capsys.readouterr().out
    assert "[snap-detect]" in out
    assert "Browser: chromium (snap)" in out
    assert "Snap confinement prevents CDP binding" in out
    assert "docs/snap-linux-headless.md" in out


def test_run_doctor_skips_snap_detect_on_non_linux(monkeypatch, capsys):
    monkeypatch.setattr(admin, "_version", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_install_mode", lambda: "git")
    monkeypatch.setattr(admin, "_chrome_running", lambda: True)
    monkeypatch.setattr(admin, "daemon_alive", lambda: True)
    monkeypatch.setattr(admin, "browser_connections", lambda: [])
    monkeypatch.setattr(admin, "_latest_release_tag", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_doctor_probe_chrome_binary_for_snap", lambda: ("chromium", "/snap/chromium/1/usr/bin/chromium"))
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)

    assert admin.run_doctor() == 0

    out = capsys.readouterr().out
    assert "[snap-detect]" not in out


def test_run_doctor_reports_bad_stored_cloud_auth_without_crashing(monkeypatch, capsys):
    monkeypatch.setattr(admin, "_version", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_install_mode", lambda: "git")
    monkeypatch.setattr(admin, "_chrome_running", lambda: True)
    monkeypatch.setattr(admin, "daemon_alive", lambda: True)
    monkeypatch.setattr(admin, "browser_connections", lambda: [])
    monkeypatch.setattr(admin, "_latest_release_tag", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_doctor_probe_chrome_binary_for_snap", lambda: (None, None))
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(admin.auth, "auth_status", lambda: (_ for _ in ()).throw(admin.auth.AuthError("auth file is not valid JSON")))

    assert admin.run_doctor() == 0

    out = capsys.readouterr().out
    assert "Browser Use cloud auth" in out
    assert "auth file is not valid JSON" in out


def test_run_doctor_fix_snap_prints_steps(capsys):
    assert admin.run_doctor_fix_snap() == 0
    out = capsys.readouterr().out
    assert "browser-harness doctor --fix-snap" in out
    assert "BH_CHROME_PATH" in out
    assert "google-chrome-stable_current_amd64.deb" in out
    assert "browser-harness --doctor" in out


def test_run_doctor_prints_active_browser_connections_and_active_pages(monkeypatch, capsys):
    monkeypatch.setattr(admin, "_version", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_install_mode", lambda: "git")
    monkeypatch.setattr(admin, "_chrome_running", lambda: True)
    monkeypatch.setattr(admin, "daemon_alive", lambda: True)
    monkeypatch.setattr(admin, "browser_connections", lambda: [
        {
            "name": "default",
            "page": {"title": "Example", "url": "https://example.test"},
        },
        {
            "name": "cats",
            "page": {"title": "Cat - Wikipedia", "url": "https://en.wikipedia.org/wiki/Cat"},
        },
    ])
    monkeypatch.setattr(admin, "_latest_release_tag", lambda: "0.1.0")
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)

    assert admin.run_doctor() == 0

    out = capsys.readouterr().out
    assert "[ok  ] active browser connections — 2" in out
    assert "        default — active page: Example — https://example.test" in out
    assert "        cats — active page: Cat - Wikipedia — https://en.wikipedia.org/wiki/Cat" in out


def test_doctor_page_output_truncates_long_text(monkeypatch, capsys):
    monkeypatch.setattr(admin, "_version", lambda: "0.1.0")
    monkeypatch.setattr(admin, "_install_mode", lambda: "git")
    monkeypatch.setattr(admin, "_chrome_running", lambda: True)
    monkeypatch.setattr(admin, "daemon_alive", lambda: True)
    monkeypatch.setattr(admin, "DOCTOR_TEXT_LIMIT", 20)
    monkeypatch.setattr(admin, "browser_connections", lambda: [
        {
            "name": "default",
            "page": {"title": "A very long page title", "url": "https://example.test/very/long/path"},
        }
    ])
    monkeypatch.setattr(admin, "_latest_release_tag", lambda: "0.1.0")
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)

    assert admin.run_doctor() == 0

    out = capsys.readouterr().out
    assert "A very long page ..." in out
    assert "https://example.t..." in out


def test_start_remote_daemon_stops_created_browser_when_daemon_start_fails(monkeypatch):
    calls = []
    browser = {"id": "browser-123", "cdpUrl": "http://127.0.0.1:9333", "liveUrl": "https://live.example"}

    def fake_browser_use(path, method, body=None):
        calls.append((path, method, body))
        if (path, method) == ("/browsers", "POST"):
            return browser
        if (path, method) == ("/browsers/browser-123", "PATCH"):
            return {}
        raise AssertionError((path, method, body))

    monkeypatch.setattr(admin, "daemon_alive", lambda name: False)
    monkeypatch.setattr(admin, "_browser_use", fake_browser_use)
    monkeypatch.setattr(admin, "_cdp_ws_from_url", lambda url: "ws://example.test/devtools/browser/1")
    monkeypatch.setattr(admin, "ensure_daemon", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        admin.start_remote_daemon()

    assert calls == [
        ("/browsers", "POST", {}),
        ("/browsers/browser-123", "PATCH", {"action": "stop"}),
    ]


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_start_remote_daemon_stops_created_browser_when_daemon_start_is_interrupted(monkeypatch, exc_type):
    calls = []
    browser = {"id": "browser-123", "cdpUrl": "http://127.0.0.1:9333", "liveUrl": "https://live.example"}

    def fake_browser_use(path, method, body=None):
        calls.append((path, method, body))
        if (path, method) == ("/browsers", "POST"):
            return browser
        if (path, method) == ("/browsers/browser-123", "PATCH"):
            return {}
        raise AssertionError((path, method, body))

    monkeypatch.setattr(admin, "daemon_alive", lambda name: False)
    monkeypatch.setattr(admin, "_browser_use", fake_browser_use)
    monkeypatch.setattr(admin, "_cdp_ws_from_url", lambda url: "ws://example.test/devtools/browser/1")
    monkeypatch.setattr(admin, "ensure_daemon", lambda **kwargs: (_ for _ in ()).throw(exc_type()))

    with pytest.raises(exc_type):
        admin.start_remote_daemon()

    assert calls == [
        ("/browsers", "POST", {}),
        ("/browsers/browser-123", "PATCH", {"action": "stop"}),
    ]


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_stop_cloud_browser_swallows_baseexception_from_stop_request(monkeypatch, exc_type):
    monkeypatch.setattr(admin, "_browser_use", lambda *args, **kwargs: (_ for _ in ()).throw(exc_type()))

    admin._stop_cloud_browser("browser-123")

def test_start_remote_daemon_does_not_stop_created_browser_on_success(monkeypatch):
    calls = []
    browser = {"id": "browser-123", "cdpUrl": "http://127.0.0.1:9333", "liveUrl": "https://live.example"}

    def fake_browser_use(path, method, body=None):
        calls.append((path, method, body))
        if (path, method) == ("/browsers", "POST"):
            return browser
        raise AssertionError((path, method, body))

    monkeypatch.setattr(admin, "daemon_alive", lambda name: False)
    monkeypatch.setattr(admin, "_browser_use", fake_browser_use)
    monkeypatch.setattr(admin, "_cdp_ws_from_url", lambda url: "ws://example.test/devtools/browser/1")
    monkeypatch.setattr(admin, "ensure_daemon", lambda **kwargs: None)
    monkeypatch.setattr(admin, "_show_live_url", lambda url: None)

    assert admin.start_remote_daemon() == browser
    assert calls == [
        ("/browsers", "POST", {}),
    ]


# --- restart_daemon: PID-reuse safety ---

def test_restart_daemon_does_not_signal_when_daemon_unreachable(monkeypatch, tmp_path):
    """If ipc.identify() returns None (daemon gone), restart_daemon must NOT
    fall back to reading the pid file and SIGTERMing whatever owns that PID —
    that's the PID-reuse hazard. It should only clean up files."""
    pid_path = tmp_path / "default.pid"
    # A pid file with a PID that, if signaled, would hit an unrelated process.
    # The whole point is that we don't read or trust this number.
    pid_path.write_text("99999")

    kill_calls = []
    monkeypatch.setattr(admin.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr(admin.ipc, "identify", lambda name, timeout=5.0: None)
    monkeypatch.setattr(admin.ipc, "ping", lambda name, timeout=1.0: False)
    monkeypatch.setattr(admin.ipc, "pid_path", lambda name: pid_path)
    monkeypatch.setattr(admin.ipc, "cleanup_endpoint", lambda name: None)

    # Should not raise, should not signal, should still clean up the pid file.
    admin.restart_daemon("default")

    assert kill_calls == [], (
        f"restart_daemon SIGTERM'd a PID despite identify() returning None — "
        f"this is the PID-reuse hazard the function is meant to avoid. Calls: {kill_calls}"
    )
    assert not pid_path.exists(), "stale pid file should be cleaned up"


def test_restart_daemon_signals_pid_returned_by_identify_not_pid_file(monkeypatch, tmp_path):
    """The PID we signal must come from the live daemon's self-report, never
    from the pid file. If a stale pid file disagrees, the live daemon's PID wins."""
    import signal

    pid_path = tmp_path / "default.pid"
    pid_path.write_text("99999")  # bogus stale value — must be ignored

    live_pid = 4242

    kill_calls = []
    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # First os.kill(pid, 0) probe: report process is gone so we exit the loop
        # without escalating. We just want to see WHICH pid was probed.
        if sig == 0:
            raise ProcessLookupError

    class FakeIPC:
        def __init__(self):
            self.shutdown_sent = False
        def identify(self, name, timeout=5.0):
            return live_pid
        def connect(self, name, timeout):
            return ("conn", "tok")
        def request(self, conn, tok, msg):
            if msg.get("meta") == "shutdown":
                self.shutdown_sent = True
            return {"ok": True}
        def pid_path(self, name):
            return pid_path
        def cleanup_endpoint(self, name):
            pass

    fake = FakeIPC()
    monkeypatch.setattr(admin.os, "kill", fake_kill)
    monkeypatch.setattr(admin.ipc, "identify", fake.identify)
    monkeypatch.setattr(admin.ipc, "ping", lambda name, timeout=1.0: True)
    monkeypatch.setattr(admin.ipc, "connect", fake.connect)
    monkeypatch.setattr(admin.ipc, "request", fake.request)
    monkeypatch.setattr(admin.ipc, "pid_path", fake.pid_path)
    monkeypatch.setattr(admin.ipc, "cleanup_endpoint", fake.cleanup_endpoint)

    admin.restart_daemon("default")

    assert fake.shutdown_sent, "expected shutdown IPC to be sent"
    assert kill_calls, "expected at least one os.kill probe"
    pids_signaled = {pid for pid, _ in kill_calls}
    assert pids_signaled == {live_pid}, (
        f"restart_daemon must only signal the PID returned by identify(); "
        f"signaled pids: {pids_signaled}, expected {{{live_pid}}} (and NOT 99999)"
    )
    assert not pid_path.exists()


def test_restart_daemon_sends_shutdown_to_pre_upgrade_daemon_without_pid_in_ping(monkeypatch, tmp_path):
    """Backward compat: a pre-upgrade daemon's ping reply has {pong:True} but
    no `pid` field, so identify() returns None. The shutdown IPC must STILL be
    sent (so the daemon exits cleanly), but no os.kill happens (we have no
    verified PID to safely signal)."""
    pid_path = tmp_path / "default.pid"
    pid_path.write_text("99999")  # bogus stale value

    kill_calls = []
    shutdown_calls = []

    def fake_request(conn, tok, msg):
        if msg.get("meta") == "shutdown":
            shutdown_calls.append(msg)
        return {"ok": True}

    monkeypatch.setattr(admin.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr(admin.ipc, "identify", lambda name, timeout=5.0: None)
    monkeypatch.setattr(admin.ipc, "ping", lambda name, timeout=1.0: True)  # old daemon: alive but no pid
    monkeypatch.setattr(admin.ipc, "connect", lambda name, timeout: ("conn", "tok"))
    monkeypatch.setattr(admin.ipc, "request", fake_request)
    monkeypatch.setattr(admin.ipc, "pid_path", lambda name: pid_path)
    monkeypatch.setattr(admin.ipc, "cleanup_endpoint", lambda name: None)

    admin.restart_daemon("default")

    assert shutdown_calls, (
        "restart_daemon must send shutdown IPC to a pre-upgrade daemon even "
        "when identify() can't return a PID — otherwise upgrades orphan the "
        "old daemon while deleting its socket and pid file."
    )
    assert kill_calls == [], (
        f"no os.kill should fire when we don't have a verified PID, "
        f"but got: {kill_calls}"
    )
    assert not pid_path.exists()


def test_restart_daemon_skips_sigterm_if_pid_was_reused_during_wait(monkeypatch, tmp_path):
    """A second identify() runs immediately before the SIGTERM. If the daemon
    exited and the PID was reused mid-wait, identify() will return None (or a
    different PID) and we must NOT signal — that's the PID-reuse race during
    the 15s wait window."""
    import signal

    pid_path = tmp_path / "default.pid"
    pid_path.write_text("99999")
    live_pid = 4242

    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # All os.kill(pid, 0) probes succeed → loop exhausts → reaches the
        # SIGTERM branch. (We're simulating a "wedged" daemon that the wait
        # loop can't tell apart from a daemon whose PID got reused.)

    # First identify() call (top of restart_daemon) returns the live PID.
    # Second identify() call (right before SIGTERM) returns None — simulating
    # the daemon having exited and its PID having been reused by an unrelated
    # process. The function must NOT escalate to SIGTERM in that state.
    identify_responses = iter([live_pid, None])
    monkeypatch.setattr(admin.os, "kill", fake_kill)
    monkeypatch.setattr(admin.ipc, "identify", lambda name, timeout=5.0: next(identify_responses))
    monkeypatch.setattr(admin.ipc, "ping", lambda name, timeout=1.0: True)
    monkeypatch.setattr(admin.ipc, "connect", lambda name, timeout: ("conn", "tok"))
    monkeypatch.setattr(admin.ipc, "request", lambda conn, tok, msg: {"ok": True})
    monkeypatch.setattr(admin.ipc, "pid_path", lambda name: pid_path)
    monkeypatch.setattr(admin.ipc, "cleanup_endpoint", lambda name: None)
    # Speed up the wait loop so the test finishes quickly. The loop polls 75
    # times at 0.2s = 15s; with sleep neutralized it runs in microseconds.
    monkeypatch.setattr(admin.time, "sleep", lambda _s: None)

    admin.restart_daemon("default")

    sigterms = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGTERM]
    assert sigterms == [], (
        f"restart_daemon issued SIGTERM despite the re-verify identify() "
        f"returning None (PID was reused during the 15s wait). Calls: {kill_calls}"
    )
    assert not pid_path.exists()


def test_restart_daemon_sigterms_via_start_time_fingerprint_when_socket_gone(monkeypatch, tmp_path):
    """Slow-shutdown recovery: the daemon's serve() tears down the IPC socket
    BEFORE the process exits (the daemon then runs slow cleanup like remote
    `stop` PATCH calls that can hang). In that window, identify() returns None
    even though the process is still our daemon. SIGTERM must still fire when
    the PID's start-time fingerprint hasn't changed since we first identified
    it — that's strong evidence of "same process, just slow to exit."
    """
    import signal

    pid_path = tmp_path / "default.pid"
    pid_path.write_text("99999")
    live_pid = 4242

    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # All os.kill(pid, 0) probes succeed; loop exhausts → SIGTERM gate runs.

    # First identify() returns live_pid. Second identify() returns None — the
    # daemon has torn down its IPC during shutdown but the process is still
    # finishing up cleanup work, so the start-time fingerprint is unchanged.
    identify_responses = iter([live_pid, None])
    # Both _process_start_time() calls return the same fingerprint, signaling
    # "still the same process." This is the legitimate-slow-shutdown case.
    monkeypatch.setattr(admin, "_process_start_time", lambda pid: "STARTED_AT_X")
    monkeypatch.setattr(admin.os, "kill", fake_kill)
    monkeypatch.setattr(admin.ipc, "identify", lambda name, timeout=5.0: next(identify_responses))
    monkeypatch.setattr(admin.ipc, "ping", lambda name, timeout=1.0: True)
    monkeypatch.setattr(admin.ipc, "connect", lambda name, timeout: ("conn", "tok"))
    monkeypatch.setattr(admin.ipc, "request", lambda conn, tok, msg: {"ok": True})
    monkeypatch.setattr(admin.ipc, "pid_path", lambda name: pid_path)
    monkeypatch.setattr(admin.ipc, "cleanup_endpoint", lambda name: None)
    monkeypatch.setattr(admin.time, "sleep", lambda _s: None)

    admin.restart_daemon("default")

    sigterms = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGTERM]
    assert sigterms == [(live_pid, signal.SIGTERM)], (
        f"slow-shutdown daemon (identify=None but unchanged start-time) must "
        f"still receive SIGTERM. signal calls: {kill_calls}"
    )


def test_restart_daemon_skips_sigterm_when_start_time_changed_during_wait(monkeypatch, tmp_path):
    """If the start-time fingerprint of the original PID has CHANGED, the PID
    was reused by another process. Even though identify() also returns None,
    we must skip SIGTERM — start-time mismatch is the signal that protects
    against killing an unrelated reused-PID process."""
    import signal

    pid_path = tmp_path / "default.pid"
    pid_path.write_text("99999")
    live_pid = 4242

    kill_calls = []
    monkeypatch.setattr(admin.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

    identify_responses = iter([live_pid, None])
    # First start-time read at top of restart_daemon: "ORIGINAL".
    # Second start-time read in the safety gate: "DIFFERENT" — proof of reuse.
    start_time_responses = iter(["ORIGINAL", "DIFFERENT"])
    monkeypatch.setattr(admin, "_process_start_time", lambda pid: next(start_time_responses))
    monkeypatch.setattr(admin.ipc, "identify", lambda name, timeout=5.0: next(identify_responses))
    monkeypatch.setattr(admin.ipc, "ping", lambda name, timeout=1.0: True)
    monkeypatch.setattr(admin.ipc, "connect", lambda name, timeout: ("conn", "tok"))
    monkeypatch.setattr(admin.ipc, "request", lambda conn, tok, msg: {"ok": True})
    monkeypatch.setattr(admin.ipc, "pid_path", lambda name: pid_path)
    monkeypatch.setattr(admin.ipc, "cleanup_endpoint", lambda name: None)
    monkeypatch.setattr(admin.time, "sleep", lambda _s: None)

    admin.restart_daemon("default")

    sigterms = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGTERM]
    assert sigterms == [], (
        f"start-time mismatch indicates PID reuse — restart_daemon must NOT "
        f"SIGTERM. signal calls: {kill_calls}"
    )


# --- _process_start_time helper ---

def test_process_start_time_returns_stable_fingerprint_for_self():
    """The start-time of the current process should be readable on Linux,
    macOS, and Windows, and stable across two reads."""
    import os as _os, sys
    if sys.platform.startswith("linux") or sys.platform == "darwin" or sys.platform == "win32":
        pid = _os.getpid()
        first = admin._process_start_time(pid)
        second = admin._process_start_time(pid)
        assert first is not None, "expected a fingerprint for the current PID"
        assert first == second, (
            f"two reads of the same PID should return the same fingerprint; "
            f"got {first!r} vs {second!r}"
        )


def test_process_start_time_returns_none_for_invalid_pid():
    """Bad inputs (None, 0, negatives, non-int) and PIDs with no live process
    must return None rather than raising."""
    for bad in (None, 0, -1, -42, "not-an-int", 1.5, True, False):
        assert admin._process_start_time(bad) is None, (
            f"expected None for invalid pid {bad!r}"
        )
    # 2**31 - 1 is the largest pid_t; in practice no live process at that PID.
    assert admin._process_start_time((1 << 31) - 1) is None


# --- _open_chrome_inspect: never hand a chrome: URL to the OS shell ---

class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _spy_open_calls(monkeypatch):
    """Record every way _open_chrome_inspect could try to open a URL."""
    import subprocess, webbrowser
    calls = {"webbrowser": [], "popen": [], "run": []}
    monkeypatch.setattr(webbrowser, "open", lambda url, **kw: calls["webbrowser"].append(url))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls["popen"].append((argv, kw)))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls["run"].append(argv))
    return calls


def test_open_chrome_inspect_is_silent_when_unattended(monkeypatch):
    """Cron/CI runs have nobody to tick the checkbox. Opening anything there is
    noise — on Windows the chrome: scheme has no shell handler, so it raises a
    modal app-picker on every scheduled run."""
    import sys
    monkeypatch.delenv("BH_OPEN_INSPECT", raising=False)
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, _Stream(tty=False))
    calls = _spy_open_calls(monkeypatch)

    admin._open_chrome_inspect()

    assert calls == {"webbrowser": [], "popen": [], "run": []}, (
        f"unattended run must not open anything; got {calls}"
    )


def test_open_chrome_inspect_ignores_a_tty_stdin(monkeypatch):
    """The script itself arrives on stdin, so a tty there says nothing about
    whether a human is watching the output. Only stdout/stderr do."""
    import sys
    monkeypatch.delenv("BH_OPEN_INSPECT", raising=False)
    monkeypatch.setattr(sys, "stdin", _Stream(tty=True))
    for name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, name, _Stream(tty=False))
    calls = _spy_open_calls(monkeypatch)

    admin._open_chrome_inspect()

    assert calls == {"webbrowser": [], "popen": [], "run": []}, (
        f"captured output means nobody is watching; got {calls}"
    )


def test_open_chrome_inspect_on_windows_passes_url_to_chrome_argv(monkeypatch):
    """Windows has no handler for the chrome: scheme, so ShellExecute (what
    webbrowser falls back to) can never work. Chrome opens it as an argv."""
    import platform
    monkeypatch.setenv("BH_OPEN_INSPECT", "1")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(admin, "_windows_chrome_exe", lambda: r"C:\chrome.exe")
    calls = _spy_open_calls(monkeypatch)

    admin._open_chrome_inspect()

    import subprocess
    assert calls["webbrowser"] == [], "must never ShellExecute a chrome: URL on Windows"
    assert len(calls["popen"]) == 1
    argv, kwargs = calls["popen"][0]
    assert argv == [r"C:\chrome.exe", "chrome://inspect/#remote-debugging"]
    # Chrome must not inherit our pipes: a parent reading stdout to EOF would
    # otherwise block until the browser exits.
    for stream in ("stdin", "stdout", "stderr"):
        assert kwargs.get(stream) is subprocess.DEVNULL, f"{stream} must be DEVNULL, got {kwargs}"


def test_open_chrome_inspect_on_windows_without_chrome_stays_silent(monkeypatch):
    """No Chrome found → still no ShellExecute fallback, or the popup is back."""
    import platform
    monkeypatch.setenv("BH_OPEN_INSPECT", "1")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(admin, "_windows_chrome_exe", lambda: None)
    calls = _spy_open_calls(monkeypatch)

    admin._open_chrome_inspect()

    assert calls == {"webbrowser": [], "popen": [], "run": []}, (
        f"no Chrome binary must mean no open attempt at all; got {calls}"
    )


def test_open_chrome_inspect_env_override_off_beats_a_tty(monkeypatch):
    import sys
    monkeypatch.setenv("BH_OPEN_INSPECT", "0")
    for name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, name, _Stream(tty=True))
    calls = _spy_open_calls(monkeypatch)

    admin._open_chrome_inspect()

    assert calls == {"webbrowser": [], "popen": [], "run": []}


def test_open_chrome_inspect_opens_for_an_interactive_posix_user(monkeypatch):
    """The interactive nudge still works where the shell can handle it."""
    import platform, sys
    monkeypatch.delenv("BH_OPEN_INSPECT", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(sys, "stdin", _Stream(tty=False))
    for name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, name, _Stream(tty=True))
    calls = _spy_open_calls(monkeypatch)

    admin._open_chrome_inspect()

    assert calls["webbrowser"] == ["chrome://inspect/#remote-debugging"]


# --- _windows_chrome_exe ---

def test_windows_chrome_exe_prefers_env_override(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("BH_CHROME_PATH", str(exe))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "nowhere"))

    assert admin._windows_chrome_exe() == str(exe)


def test_windows_chrome_exe_skips_a_directory_and_a_bad_tilde(monkeypatch, tmp_path):
    """A dir, or a ~user that has no home (RuntimeError, not OSError), must fall
    through rather than crash ensure_daemon's retry path."""
    (tmp_path / "chrome.exe").mkdir()
    monkeypatch.setenv("BH_CHROME_PATH", str(tmp_path / "chrome.exe"))
    monkeypatch.setenv("CHROME_PATH", "~nosuchuser-really/chrome.exe")
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)

    assert admin._windows_chrome_exe() is None


def test_windows_chrome_exe_falls_back_to_standard_install_dir(monkeypatch, tmp_path):
    installed = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    for var in ("BH_CHROME_PATH", "CHROME_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert admin._windows_chrome_exe() == str(installed)


def test_windows_chrome_exe_returns_none_when_chrome_is_absent(monkeypatch, tmp_path):
    for var in ("BH_CHROME_PATH", "CHROME_PATH"):
        monkeypatch.delenv(var, raising=False)
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "empty"))

    assert admin._windows_chrome_exe() is None
