import ctypes
import os
import sys
import time

import pytest

from winSync.gui import (PORT_BUSY_RE, ServerProcess, SetupError, format_version, page_url,
                         read_cat_port, read_version)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Like the real server (python -m winSync --exit-with-stdin): exits when its stdin closes.
STDIN_WATCHER = (f"import sys, time; sys.path.insert(0, {ROOT!r})\n"
                 "from winSync.__main__ import exit_when_parent_goes\n"
                 "exit_when_parent_goes()\n")


def child(code, tmp_path, **kw):
    return ServerProcess(root=tmp_path, argv=[sys.executable, "-u", "-c", code], **kw)


def collect(sp, timeout=10):
    """Drain events until the exit event; return (lines, returncode)."""
    lines, deadline = [], time.time() + timeout
    while time.time() < deadline:
        kind, value = sp.events.get(timeout=timeout)
        if kind == "line":
            lines.append(value)
        else:
            return lines, value
    raise AssertionError("no exit event")


def alive(pid):
    """Without os.kill(pid, 0), which on Windows terminates the process."""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259                     # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def test_control_flag_added_only_when_requested(tmp_path, monkeypatch):
    seen = []
    import winSync.gui as gui
    monkeypatch.setattr(gui.subprocess, "Popen",
                        lambda argv, **kw: seen.append(argv) or (_ for _ in ()).throw(OSError("stop")))
    sp = ServerProcess(root=tmp_path)
    monkeypatch.setattr(sp, "venv_python", lambda: tmp_path / "python.exe")
    for control in (None, "both", "radio"):
        with pytest.raises(OSError):
            sp.start(control=control)
    assert seen[0][-1] == "--exit-with-stdin"
    assert seen[1][-2:] == ["--control", "both"] and seen[2][-2:] == ["--control", "radio"]


def test_read_control(tmp_path):
    from winSync.gui import read_control
    assert read_control(tmp_path) == "radio"                       # no config
    for value in ("radio", "kiwi", "both"):
        (tmp_path / "config.toml").write_text(f'[sync]\ncontrol = "{value}"\n')
        assert read_control(tmp_path) == value
    (tmp_path / "config.toml").write_text('[sync]\ncontrol = "everything"\n')
    assert read_control(tmp_path) == "radio"
    (tmp_path / "config.toml").write_text("not [valid toml")
    assert read_control(tmp_path) == "radio"


def test_follow_flag_added_only_when_requested(tmp_path, monkeypatch):
    seen = []
    import winSync.gui as gui
    monkeypatch.setattr(gui.subprocess, "Popen",
                        lambda argv, **kw: seen.append((argv, kw)) or (_ for _ in ()).throw(OSError("stop")))
    sp = ServerProcess(root=tmp_path)
    monkeypatch.setattr(sp, "venv_python", lambda: tmp_path / "python.exe")
    for follow in (None, "B"):
        with pytest.raises(OSError):
            sp.start(follow)
    assert seen[0][0][-4:] == ["-u", "-m", "winSync", "--exit-with-stdin"]
    assert seen[1][0][-2:] == ["--follow", "B"]
    assert seen[0][1]["creationflags"] & 0x08000000         # CREATE_NO_WINDOW: no console pops up


def test_streams_output_live_and_reports_exit_code(tmp_path):
    sp = child("import time\nprint('one'); time.sleep(0.3); print('two'); raise SystemExit(3)", tmp_path)
    sp.start()
    kind, value = sp.events.get(timeout=5)
    assert (kind, value) == ("line", "one")          # arrives before the child has finished
    assert sp.running
    lines, rc = collect(sp)
    assert lines == ["two"] and rc == 3
    assert not sp.running


def test_runs_in_project_root_and_merges_stderr(tmp_path):
    sp = child("import os, sys\nprint(os.getcwd()); print('oops', file=sys.stderr)", tmp_path)
    sp.start()
    lines, rc = collect(sp)
    assert lines == [str(tmp_path.resolve()), "oops"] and rc == 0


def test_stop_closes_stdin_and_the_server_exits_promptly(tmp_path):
    sp = child(STDIN_WATCHER + "print('up', flush=True)\ntime.sleep(60)", tmp_path)
    sp.start()
    assert sp.events.get(timeout=5) == ("line", "up")
    t0 = time.time()
    sp.stop(blocking=True)
    lines, rc = collect(sp)
    assert rc == 0 and time.time() - t0 < 3
    assert not sp.running


def test_stop_kills_a_child_that_ignores_stdin(tmp_path):
    sp = child("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(60)", tmp_path,
               kill_after=0.5)
    sp.start()
    kind, pid = sp.events.get(timeout=5)
    pid = int(pid)       # the interpreter itself: a venv's python.exe is a launcher in front of it
    t0 = time.time()
    sp.stop(blocking=True)
    lines, rc = collect(sp)
    assert rc != 0 and time.time() - t0 < 4
    assert not sp.running
    for _ in range(40):
        if not alive(pid):
            break
        time.sleep(0.05)
    assert not alive(pid)


def test_server_dies_if_the_window_process_is_killed(tmp_path):
    """A crash or Task Manager kill of the window must not leave a server holding the port."""
    import subprocess
    helper = (
        "import os, sys\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from winSync.gui import ServerProcess\n"
        f"code = {STDIN_WATCHER!r} + 'import os; print(os.getpid(), flush=True); time.sleep(60)'\n"
        f"sp = ServerProcess(root={str(tmp_path)!r}, argv=[sys.executable, '-c', code])\n"
        "sp.start()\n"
        "print(sp.events.get(timeout=5)[1], flush=True)\n"
        "os._exit(9)\n"                              # no cleanup at all, like a crash
    )
    out = subprocess.run([sys.executable, "-c", helper], capture_output=True, text=True, timeout=20)
    pid = int(out.stdout.split()[0])
    for _ in range(100):
        if not alive(pid):
            break
        time.sleep(0.05)
    assert not alive(pid)


def test_port_busy_is_recognised():
    assert PORT_BUSY_RE.search("OSError: [Errno 10048] error while attempting to bind on address "
                               "('127.0.0.1', 8090): only one usage of each socket address "
                               "(protocol/network address/port) is normally permitted")
    assert not PORT_BUSY_RE.search("winSync: open http://127.0.0.1:8090/_winsync/")


def test_restart_uses_a_fresh_event_queue(tmp_path):
    sp = child("print('hi')", tmp_path)
    sp.start()
    collect(sp)
    first = sp.events
    sp.start()
    assert sp.events is not first
    assert collect(sp) == (["hi"], 0)


def test_missing_venv_raises_a_clear_setup_error(tmp_path):
    with pytest.raises(SetupError, match="winSync.cmd"):
        ServerProcess(root=tmp_path).start()


def test_stop_when_not_running_is_a_noop(tmp_path):
    ServerProcess(root=tmp_path).stop(blocking=True)


def test_read_cat_port(tmp_path):
    assert read_cat_port(tmp_path) == 13013                       # no config
    (tmp_path / "config.toml").write_text("[cat]\nport = 19191\n")
    assert read_cat_port(tmp_path) == 19191
    (tmp_path / "config.toml").write_text("not [valid toml")
    assert read_cat_port(tmp_path) == 13013


def test_page_url_from_config(tmp_path):
    assert page_url(tmp_path) == "http://127.0.0.1:8090/_winsync/"          # no config: defaults
    cases = {
        '[server]\nhost = "127.0.0.1"\nport = 8090\n': "http://127.0.0.1:8090/_winsync/",
        '[server]\nhost = "0.0.0.0"\nport = 9000\n': "http://127.0.0.1:9000/_winsync/",   # all interfaces
        '[server]\nhost = "::"\nport = 9000\n': "http://127.0.0.1:9000/_winsync/",
        '[server]\nhost = "192.168.1.5"\nport = 8091\n': "http://192.168.1.5:8091/_winsync/",
        '[server]\nhost = "::1"\nport = 8090\n': "http://[::1]:8090/_winsync/",
        '[server]\nport = 8123\n': "http://127.0.0.1:8123/_winsync/",
    }
    for text, expected in cases.items():
        (tmp_path / "config.toml").write_text(text)
        assert page_url(tmp_path) == expected, text
    (tmp_path / "config.toml").write_text("not [valid toml")
    assert page_url(tmp_path) == "http://127.0.0.1:8090/_winsync/"


def test_format_version():
    assert format_version("0.3") == "v0.30"
    assert format_version("1") == "v1.00"
    assert format_version("2.5") == "v2.50"
    assert format_version("v0.3") == "v0.30"
    assert format_version("V1.2") == "v1.20"
    assert format_version("  0.3 \n") == "v0.30"
    assert format_version("1.2.3") == "v1.2.3"          # not "x.xx": shown as-is, just prefixed
    assert format_version("v1.2.3") == "v1.2.3"          # already has a 'v': not doubled
    assert format_version("") is None
    assert format_version("   ") is None


def test_read_version(tmp_path):
    assert read_version(tmp_path) is None                 # no VERSION file
    (tmp_path / "VERSION").write_text("0.3\n")
    assert read_version(tmp_path) == "v0.30"
    (tmp_path / "VERSION").write_text("")
    assert read_version(tmp_path) is None


def test_source_and_rig_flags(tmp_path, monkeypatch):
    seen = []
    import winSync.gui as gui
    monkeypatch.setattr(gui.subprocess, "Popen",
                        lambda argv, **kw: seen.append(argv) or (_ for _ in ()).throw(OSError("stop")))
    sp = ServerProcess(root=tmp_path)
    monkeypatch.setattr(sp, "venv_python", lambda: tmp_path / "python.exe")
    for source, rig in gui.SOURCE_CHOICES:
        with pytest.raises(OSError):
            sp.start(control="radio", source=source, rig=rig)
    assert seen[0][-4:] == ["--control", "radio", "--source", "thetis"]
    assert seen[1][-4:] == ["--source", "omnirig", "--rig", "1"]
    assert seen[2][-4:] == ["--source", "omnirig", "--rig", "2"]


def test_read_source(tmp_path):
    from winSync.gui import SOURCE_CHOICES, read_source
    assert read_source(tmp_path) == ("thetis", None)
    cases = {'[cat]\nsource = "thetis"\n': ("thetis", None),
             '[cat]\nsource = "omnirig"\n': ("omnirig", 1),
             '[cat]\nsource = "omnirig"\n[omnirig]\nrig = 2\n': ("omnirig", 2),
             '[cat]\nsource = "omnirig"\n[omnirig]\nrig = 7\n': ("omnirig", 1),
             '[cat]\nsource = "flrig"\n': ("thetis", None)}
    for text, expected in cases.items():
        (tmp_path / "config.toml").write_text(text)
        assert read_source(tmp_path) == expected and expected in SOURCE_CHOICES


def test_sdrconsole_source(tmp_path, monkeypatch):
    import winSync.gui as gui
    assert gui.SOURCE_CHOICES[("sdrconsole", None)] == "SDR Console"
    assert list(gui.SOURCE_CHOICES)[0] == ("thetis", None)          # Thetis is still the first choice
    (tmp_path / "config.toml").write_text('[cat]\nsource = "sdrconsole"\n')
    assert gui.read_source(tmp_path) == ("sdrconsole", None)
    assert gui.read_sdrconsole_port(tmp_path) == "COM23"
    (tmp_path / "config.toml").write_text('[cat]\nsource = "sdrconsole"\n[sdrconsole]\nport = "COM9"\n')
    assert gui.read_sdrconsole_port(tmp_path) == "COM9"
    seen = []
    monkeypatch.setattr(gui.subprocess, "Popen",
                        lambda argv, **kw: seen.append(argv) or (_ for _ in ()).throw(OSError("stop")))
    sp = ServerProcess(root=tmp_path)
    monkeypatch.setattr(sp, "venv_python", lambda: tmp_path / "python.exe")
    with pytest.raises(OSError):
        sp.start(control="radio", source="sdrconsole", rig=None)
    assert seen[0][-4:] == ["--control", "radio", "--source", "sdrconsole"]


def test_launcher_checks_every_requirement():
    """winSync.cmd runs the setup when an import fails, so a v1.2 install (no pyserial) gets pyserial
    the next time it starts. The launcher's and setup.ps1's checks must import every requirement."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    reqs = {re.split(r"[<>=]", line)[0].strip() for line in (root / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")}
    assert reqs == {"aiohttp", "pywin32", "pyserial"}
    imports = {"aiohttp": "aiohttp", "pywin32": "win32com.client", "pyserial": "serial"}
    cmd = (root / "winSync.cmd").read_text()
    check = re.search(r'python\.exe" -c "import ([^"]+)" >nul 2>&1 \|\| goto setup', cmd).group(1)
    setup = re.search(r"\$VenvPy -c 'import ([^']+)'", (root / "setup.ps1").read_text()).group(1)
    for line in (check, setup):
        names = {n.strip() for n in line.split(",")}
        assert {imports[r] for r in reqs} <= names and "tkinter" in names


def test_control_labels_say_receiver():
    """The window and the page name the receiver winSync drives "receiver", not "Kiwi", while the
    config.toml values stay radio / kiwi / both so existing configs still work."""
    import re
    from pathlib import Path
    from winSync import omnirig
    from winSync.gui import CONTROL_CHOICES
    assert CONTROL_CHOICES == {"radio": "Radio controls receiver", "kiwi": "Receiver controls radio",
                               "both": "Both"}
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    select = re.search(r'<select id="control">(.*?)</select>', html).group(1)
    assert dict(re.findall(r'<option value="(\w+)">([^<]+)</option>', select)) == CONTROL_CHOICES
    assert omnirig.READ_ONLY == "winSync only reads from OmniRig, so the receiver can't change the radio"


def test_last_radio_is_kept_beside_urls_json(tmp_path, monkeypatch):
    from winSync.gui import last_radio_path
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert last_radio_path(tmp_path) == tmp_path / "Roaming" / "winSync" / "last_radio.json"
    (tmp_path / "config.toml").write_text(f'[server]\nurls_file = {str(tmp_path / "x" / "u.json")!r}\n')
    assert last_radio_path(tmp_path) == tmp_path / "x" / "last_radio.json"
    (tmp_path / "config.toml").write_text('[server]\nurls_file = "data/urls.json"\n')   # relative: as the server
    assert last_radio_path(tmp_path) == tmp_path / "data" / "last_radio.json"


def test_every_radio_choice_is_remembered(tmp_path):
    from winSync.gui import SOURCE_CHOICES, initial_source, save_last_radio
    path = tmp_path / "AppData" / "winSync" / "last_radio.json"      # folder made on first save
    (tmp_path / "config.toml").write_text('[cat]\nsource = "omnirig"\n[omnirig]\nrig = 2\n')
    for key in SOURCE_CHOICES:
        assert save_last_radio(path, key) is None
        assert initial_source(tmp_path, path) == key
    assert not path.with_name("last_radio.json.tmp").exists()


def test_first_run_or_bad_file_opens_on_config_source(tmp_path):
    from winSync.gui import initial_source, load_last_radio
    path = tmp_path / "last_radio.json"
    assert initial_source(tmp_path, path) == ("thetis", None)                 # no config, no file
    (tmp_path / "config.toml").write_text('[cat]\nsource = "sdrconsole"\n')
    assert initial_source(tmp_path, path) == ("sdrconsole", None)             # first run
    for bad in ("", "not json", "[]", "null", '"omnirig"', "{}", '{"source": "omnirig", "rig": 3}',
                '{"source": "kenwood"}', '{"source": ["thetis"]}', '{"rig": 1}'):
        path.write_text(bad)
        assert load_last_radio(path) is None, bad
        assert initial_source(tmp_path, path) == ("sdrconsole", None), bad
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert initial_source(tmp_path, path) == ("sdrconsole", None)
    path.unlink()
    path.mkdir()                                                             # unreadable: a folder
    assert initial_source(tmp_path, path) == ("sdrconsole", None)


def test_save_failure_is_reported_not_raised(tmp_path):
    from winSync.gui import save_last_radio
    (tmp_path / "file").write_text("")
    problem = save_last_radio(tmp_path / "file" / "last_radio.json", ("thetis", None))
    assert problem and problem.startswith("could not remember the Radio choice")


def test_start_remembers_the_radio(tmp_path):
    """Pressing Start saves the Radio choice (with the OmniRig rig number); a failed start doesn't."""
    from types import SimpleNamespace
    from winSync.gui import App, CONTROL_CHOICES, SOURCE_CHOICES, initial_source

    class Var:
        def __init__(self, v): self.v = v
        def get(self): return self.v

    started, lines = [], []
    def app_with(source_key, fail=None):
        def start(**kw):
            if fail:
                raise fail
            started.append(kw)
        return SimpleNamespace(
            server=SimpleNamespace(running=False, start=start), stopping=False, port_busy=False,
            last_radio=tmp_path / "w" / "last_radio.json", control=Var(CONTROL_CHOICES["radio"]),
            source=Var(SOURCE_CHOICES[source_key]), _source_key=lambda: source_key,
            messagebox=SimpleNamespace(showerror=lambda *a: lines.append(a)),
            _append=lambda text, tag=None: lines.append(text), _set_status=lambda *a: None,
            _update_buttons=lambda: None)

    App._toggle(app_with(("omnirig", 2)))
    assert started[-1] == {"control": "radio", "source": "omnirig", "rig": 2}
    assert initial_source(tmp_path, tmp_path / "w" / "last_radio.json") == ("omnirig", 2)
    App._toggle(app_with(("sdrconsole", None)))
    assert initial_source(tmp_path, tmp_path / "w" / "last_radio.json") == ("sdrconsole", None)
    App._toggle(app_with(("thetis", None), fail=OSError("no python")))         # did not start
    assert initial_source(tmp_path, tmp_path / "w" / "last_radio.json") == ("sdrconsole", None)
