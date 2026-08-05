import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

import psutil
from flask import Flask, jsonify, render_template, request
from mcstatus import JavaServer

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "servers_root": r"C:\Users\jaalf\OneDrive\Desktop\Minecraft Servers",
    "bot_dir": r"C:\Users\jaalf\Documents\Github\MinecraftServerDiscordBot",
    "playit_exe": r"C:\Program Files\playit_gg\bin\playit.exe",
    "playit_log": r"C:\ProgramData\playit_gg\logs\playitd.log",
    "host": "127.0.0.1",
    "port": 8765,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((HERE / "config.json").read_text()))
    except FileNotFoundError:
        pass
    return cfg


CONFIG = load_config()
BOT_DIR = Path(CONFIG["bot_dir"])
SERVERS_ROOT = Path(CONFIG["servers_root"])
PLAYIT_EXE = Path(CONFIG["playit_exe"])
PLAYIT_LOG = Path(CONFIG["playit_log"])

# Launch children without flashing a console window (matters under pythonw).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ---------- RCON (Source protocol) ----------

class RconError(Exception):
    pass


def _rcon_recv(sock) -> tuple:
    def read(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf
    (length,) = struct.unpack("<i", read(4))
    data = read(length)
    req_id, ptype = struct.unpack("<ii", data[:8])
    return req_id, ptype, data[8:-2].decode("utf-8", errors="replace")


def _rcon_send(sock, ptype: int, body: str, req_id: int = 0):
    data = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(data)) + data)


def rcon_command(host: str, port: int, password: str, command: str, timeout: float = 5.0) -> str:
    """Run a single command over RCON and return the server's response text."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _rcon_send(sock, 3, password)  # SERVERDATA_AUTH
        while True:  # some servers emit an empty value packet before the auth reply
            req_id, ptype, _ = _rcon_recv(sock)
            if ptype == 2:  # SERVERDATA_AUTH_RESPONSE
                if req_id == -1:
                    raise RconError("authentication failed (wrong rcon.password)")
                break
        _rcon_send(sock, 2, command)  # SERVERDATA_EXECCOMMAND
        _, _, body = _rcon_recv(sock)
        return body

# Reuse the bot's tested helpers; fall back to tiny local copies if the bot
# repo isn't where config says (dashboard still works, just less DRY).
sys.path.insert(0, str(BOT_DIR))
try:
    from monitor import format_duration, is_server_up  # type: ignore
except Exception:  # pragma: no cover - only hit on a misconfigured bot_dir
    import socket

    def is_server_up(host, port, timeout=2.0):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def format_duration(seconds):
        s = int(seconds)
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return " ".join(
            p for p in (f"{d}d" if d else "", f"{h}h" if h else "",
                        f"{m}m" if m else "", f"{s}s") if p
        ) or "0s"


# ---------- pure helpers (unit-tested in test_app.py) ----------

def read_properties(path: Path) -> dict:
    """Parse a server.properties file into a dict (key=value, ignore comments)."""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def scan_servers(root) -> list:
    """Discover server folders under root: those containing server.properties.

    Returns [{name, path, scripts:[.bat names], port}] sorted by name.
    """
    root = Path(root)
    servers = []
    if not root.is_dir():
        return servers
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        props_file = d / "server.properties"
        if not props_file.is_file():
            continue
        scripts = sorted(f.name for f in d.iterdir() if f.suffix.lower() == ".bat")
        props = read_properties(props_file)
        servers.append({
            "name": d.name,
            "path": str(d),
            "scripts": scripts,
            "port": int(props.get("server-port") or 25565),
        })
    return servers


def merge_env(text: str, updates: dict) -> str:
    """Return .env text with updates applied in place, preserving unmanaged keys
    (e.g. DISCORD_TOKEN) and comments. Missing keys are appended."""
    updates = {k: str(v) for k, v in updates.items() if v is not None and v != ""}
    seen = set()
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out_lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            out_lines.append(f"{key}={val}")
    return "\n".join(out_lines) + "\n"


# ---------- managed processes ----------

class _Managed:
    """Shared state/metrics for a process tree, keyed by self.pid."""

    def __init__(self, pid: int, label: str):
        self.pid = pid
        self.label = label
        self.started_at = time.time()
        self.log = deque(maxlen=500)
        self.stopping = False
        self.adopted = False
        self._pcache = {}  # pid -> psutil.Process, kept across polls for cpu deltas

    def kill_tree(self):
        try:
            parent = psutil.Process(self.pid)
            procs = parent.children(recursive=True) + [parent]
        except psutil.Error:
            return
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=5)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass

    def metrics(self) -> dict:
        """CPU% (of the whole machine) and RAM (MB) over the process tree.

        cpu_percent(interval=None) is a delta since the previous call on the
        *same* Process object, so we cache objects by pid across polls; the
        first poll after a process appears reads 0 until the next sample.
        """
        try:
            parent = psutil.Process(self.pid)
            procs = {parent.pid: parent}
            for c in parent.children(recursive=True):
                procs[c.pid] = c
        except psutil.Error:
            return {"cpu": 0.0, "mem_mb": 0.0}
        cpu = 0.0
        mem = 0
        cache = {}
        for pid, proc in procs.items():
            p = self._pcache.get(pid, proc)  # reuse to keep the cpu baseline
            try:
                cpu += p.cpu_percent(interval=None)
                mem += p.memory_info().rss
            except psutil.Error:
                continue
            cache[pid] = p
        self._pcache = cache
        ncpu = psutil.cpu_count() or 1
        return {"cpu": round(cpu / ncpu, 1), "mem_mb": round(mem / 1048576, 1)}


class Proc(_Managed):
    """A subprocess this dashboard launched, with a captured console log + stdin."""

    def __init__(self, popen: subprocess.Popen, label: str):
        super().__init__(popen.pid, label)
        self.popen = popen
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        assert self.popen.stdout is not None
        for line in self.popen.stdout:
            self.log.append(line.rstrip("\n"))

    def alive(self) -> bool:
        return self.popen.poll() is None

    def send(self, cmd: str):
        if self.alive() and self.popen.stdin:
            self.popen.stdin.write(cmd + "\n")
            self.popen.stdin.flush()


class AdoptedProc(_Managed):
    """A process the dashboard reconnected to on reopen (it didn't start it). We
    can read status/metrics and terminate it, but have no stdin/stdout, so the
    console log and console commands are unavailable and stop is a terminate."""

    def __init__(self, pid: int, label: str):
        super().__init__(pid, label)
        self.adopted = True
        try:
            self.started_at = psutil.Process(pid).create_time()
        except psutil.Error:
            pass
        self.log.append("(reconnected to an already-running process — "
                        "live console and commands aren't available)")

    def alive(self) -> bool:
        try:
            p = psutil.Process(self.pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def send(self, cmd: str):
        pass  # no stdin on an adopted process


LOCK = threading.Lock()
SERVER: _Managed | None = None
SERVER_META: dict = {}  # {name, port}
BOT: _Managed | None = None


def _graceful_stop(proc: _Managed, timeout: float = 90.0):
    proc.stopping = True
    if getattr(proc, "adopted", False):
        rc = _server_rcon()  # clean save via RCON if available; else terminate
        if rc:
            try:
                rcon_command(*rc, "stop")
                deadline = time.time() + timeout
                while proc.alive() and time.time() < deadline:
                    time.sleep(1)
            except (OSError, RconError):
                pass
        if proc.alive():
            proc.kill_tree()
        return
    proc.send("stop")
    deadline = time.time() + timeout
    while proc.alive() and time.time() < deadline:
        time.sleep(1)
    if proc.alive():
        proc.kill_tree()


# ---------- Flask ----------

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/servers")
def api_servers():
    running = SERVER_META.get("name") if SERVER and SERVER.alive() else None
    return jsonify({"servers": scan_servers(SERVERS_ROOT), "running": running})


def _is_server_proc(name: str, cwd: str, root: str) -> bool:
    """True if a process looks like a Minecraft server (java/launcher) whose
    working dir is inside the servers root."""
    if not cwd:
        return False
    return (name.lower() in ("java.exe", "javaw.exe", "cmd.exe")
            and cwd.lower().startswith(root.lower()))


def _kill_existing_servers() -> int:
    """Ensure only one server runs. The server we manage is stopped gracefully
    (sends 'stop' so the world saves); stray, untrackable ones can only be
    terminated. Returns how many were stopped/killed."""
    n = 0
    if SERVER and SERVER.alive():
        _graceful_stop(SERVER, timeout=90)  # protects the world of our server
        n += 1
    root = str(SERVERS_ROOT)
    victims = []
    for proc in psutil.process_iter(["name", "cwd"]):
        try:
            if _is_server_proc(proc.info["name"] or "", proc.info["cwd"] or "", root):
                victims.append(proc)
        except psutil.Error:
            continue
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=8)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return n + len(victims)


STATE_FILE = HERE / "state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _remember_server(name: str, script: str):
    try:
        STATE_FILE.write_text(json.dumps({"last_server": name, "last_script": script}))
    except OSError:
        pass


def _launch_server(match: dict, script: str) -> int:
    """Launch a server (killing any existing one first). Returns replaced count."""
    global SERVER, SERVER_META
    killed = _kill_existing_servers()  # guard: only one server at a time
    folder = Path(match["path"])
    popen = subprocess.Popen(
        ["cmd", "/c", script],
        cwd=str(folder),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=NO_WINDOW,
    )
    props = read_properties(folder / "server.properties")
    SERVER = Proc(popen, match["name"])
    SERVER_META = {
        "name": match["name"],
        "port": match["port"],
        "folder": str(folder),
        # Query (GS4) gives the full player list; the status sample is
        # capped/anonymized. Only used when the server enables it.
        "query": props.get("enable-query", "").lower() == "true",
        "query_port": int(props.get("query.port") or match["port"]),
    }
    _remember_server(match["name"], script)  # for Start All next time
    return killed


@app.post("/api/server/start")
def api_server_start():
    with LOCK:
        data = request.get_json(force=True)
        name, script = data.get("name"), data.get("script")
        match = next((s for s in scan_servers(SERVERS_ROOT) if s["name"] == name), None)
        if not match:
            return jsonify({"error": "Unknown server."}), 404
        if script not in match["scripts"]:
            return jsonify({"error": "Unknown start script."}), 400
        return jsonify({"ok": True, "replaced": _launch_server(match, script)})


def _server_rcon():
    """(host, port, password) if the current server has RCON enabled, else None.
    Read from server.properties each time so config changes are picked up."""
    folder = SERVER_META.get("folder")
    if not folder:
        return None
    props = read_properties(Path(folder) / "server.properties")
    if props.get("enable-rcon", "").lower() != "true":
        return None
    pw = props.get("rcon.password", "")
    if not pw:
        return None
    return ("127.0.0.1", int(props.get("rcon.port") or 25575), pw)


@app.post("/api/server/command")
def api_server_command():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    cmd = (request.get_json(force=True).get("cmd") or "").strip()
    if not cmd:
        return jsonify({"error": "Empty command."}), 400
    if getattr(SERVER, "adopted", False):  # no pipe; use RCON
        rc = _server_rcon()
        if not rc:
            return jsonify({"error": "Enable RCON (enable-rcon=true + rcon.password) and "
                            "restart the server to send commands to a reconnected server."}), 409
        try:
            return jsonify({"ok": True, "output": rcon_command(*rc, cmd)})
        except (OSError, RconError) as e:
            return jsonify({"error": f"RCON: {e}"}), 502
    SERVER.send(cmd)
    return jsonify({"ok": True})


@app.post("/api/server/stop")
def api_server_stop():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    threading.Thread(target=_graceful_stop, args=(SERVER,), daemon=True).start()
    return jsonify({"ok": True})


def _adopted_console() -> list:
    """Console for an adopted server, tailed from its logs/latest.log with the
    noisy RCON client connect/disconnect thread lines filtered out."""
    folder = SERVER_META.get("folder")
    if folder:
        lines = [ln for ln in _tail(Path(folder) / "logs" / "latest.log", n=600)
                 if "RCON Client" not in ln and "RCON Listener" not in ln]
        if lines:
            return lines[-300:]
    return list(SERVER.log) if SERVER else []


@app.get("/api/server/status")
def api_server_status():
    if not (SERVER and SERVER.alive()):
        state = "offline"
        if SERVER and SERVER.stopping:
            state = "offline"
        return jsonify({"state": state, "log": list(SERVER.log) if SERVER else []})

    port = SERVER_META.get("port", 25565)
    host = "127.0.0.1"
    resp = {
        "state": "stopping" if SERVER.stopping else "starting",
        "name": SERVER_META.get("name"),
        "address": f"{host}:{port}",
        "uptime": format_duration(time.time() - SERVER.started_at),
        "metrics": SERVER.metrics(),
        # Owned servers stream via the captured pipe; adopted ones have no pipe,
        # so read the console from the server's own logs/latest.log.
        "log": _adopted_console() if getattr(SERVER, "adopted", False) else list(SERVER.log),
        "adopted": getattr(SERVER, "adopted", False),
        "rcon": _server_rcon() is not None,  # config-based; no probe (avoids log spam)
    }
    if not SERVER.stopping and is_server_up(host, port):
        resp["state"] = "online"
        got_names = False
        if SERVER_META.get("query"):  # full roster via GS4 query when enabled
            try:
                q = JavaServer(host, SERVER_META.get("query_port", port), timeout=2).query()
                # mcstatus>=11 exposes the roster as .list; older used .names
                roster = getattr(q.players, "list", None)
                if roster is None:
                    roster = getattr(q.players, "names", [])
                resp["players"] = {"online": q.players.online, "max": q.players.max,
                                   "names": sorted(roster)}
                got_names = True
            except Exception:
                pass  # query enabled but not answering; fall back to the sample
        try:
            st = JavaServer(host, port, timeout=2).status()
            if not got_names:  # status sample: capped and may be "Anonymous Player"
                resp["players"] = {
                    "online": st.players.online,
                    "max": st.players.max,
                    "names": sorted(p.name for p in (st.players.sample or [])),
                }
            resp["version"] = st.version.name
            try:
                resp["motd"] = st.motd.to_plain()
            except Exception:
                resp["motd"] = str(getattr(st, "description", ""))
        except Exception:
            pass  # port open but full ping not ready yet
    return jsonify(resp)


def _bot_env_path() -> Path:
    return BOT_DIR / ".env"


def _bot_settings() -> dict:
    """Current bot .env values with the token masked."""
    text = ""
    try:
        text = _bot_env_path().read_text()
    except OSError:
        pass
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    token = vals.get("DISCORD_TOKEN", "")
    return {
        "DISCORD_TOKEN_set": bool(token),
        "CHANNEL_ID": vals.get("CHANNEL_ID", ""),
        "MC_HOST": vals.get("MC_HOST", "127.0.0.1"),
        "MC_PORT": vals.get("MC_PORT", "25565"),
        "POLL_INTERVAL": vals.get("POLL_INTERVAL", "30"),
        "GUILD_ID": vals.get("GUILD_ID", ""),
    }


@app.get("/api/bot")
def api_bot():
    return jsonify({
        "running": bool(BOT and BOT.alive()),
        "settings": _bot_settings(),
        "log": list(BOT.log) if BOT else [],
    })


@app.post("/api/bot/settings")
def api_bot_settings():
    data = request.get_json(force=True)
    keys = ("CHANNEL_ID", "MC_HOST", "MC_PORT", "POLL_INTERVAL", "GUILD_ID")
    updates = {k: data.get(k) for k in keys}
    path = _bot_env_path()
    try:
        text = path.read_text()
    except OSError:
        text = ""
    path.write_text(merge_env(text, updates))
    return jsonify({"ok": True, "settings": _bot_settings()})


def _is_bot_cmdline(cmdline) -> bool:
    """True if a process command line looks like it's running the bot script."""
    return any("bot.py" in str(a).lower() for a in (cmdline or []))


def _kill_existing_bots() -> int:
    """Kill any stray bot.py processes (launcher + child) under BOT_DIR so we
    never end up with two bots on the same token. Returns how many were killed."""
    target = str(BOT_DIR).lower()
    victims = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if not _is_bot_cmdline(proc.info["cmdline"]):
                continue
            try:
                if proc.cwd().lower() != target:
                    continue
            except (psutil.Error, OSError):
                pass  # cwd unreadable; still a bot.py match, kill it
            victims.append(proc)
        except psutil.Error:
            continue
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return len(victims)


def _launch_bot() -> int:
    """Launch the bot (killing any existing one first). Returns replaced count."""
    global BOT
    killed = _kill_existing_bots()  # guard: no duplicate bots
    popen = subprocess.Popen(
        ["py", "bot.py"],
        cwd=str(BOT_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=NO_WINDOW,
    )
    BOT = Proc(popen, "discord-bot")
    return killed


@app.post("/api/bot/start")
def api_bot_start():
    with LOCK:
        if not (BOT_DIR / "bot.py").is_file():
            return jsonify({"error": f"bot.py not found in {BOT_DIR}"}), 404
        return jsonify({"ok": True, "replaced": _launch_bot()})


@app.post("/api/bot/stop")
def api_bot_stop():
    if not (BOT and BOT.alive()):
        return jsonify({"error": "Bot not running."}), 409
    BOT.kill_tree()
    return jsonify({"ok": True})


# ---------- playit.gg ----------

def _tail(path: Path, n: int = 300, chunk: int = 64000) -> list:
    """Last n lines of a (possibly large) log file, reading only its tail."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > chunk and lines:
        lines = lines[1:]  # first line is likely partial
    return lines[-n:]


def _playit_run(*args, timeout=30) -> dict:
    if not PLAYIT_EXE.is_file():
        return {"error": f"playit not found at {PLAYIT_EXE}"}
    try:
        r = subprocess.run([str(PLAYIT_EXE), *args], capture_output=True,
                           text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip(),
            "raw": r.stdout}


def playit_status() -> dict:
    r = _playit_run("status", timeout=10)
    if "error" in r:
        return {"running": False, "error": r["error"]}
    info = {}
    for line in r["raw"].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
    phase = info.get("Phase", "")
    out = {"running": phase == "running", "phase": phase or "unknown",
           "version": info.get("Version", ""),
           "secret_configured": info.get("Secret configured", "") == "true"}
    up = info.get("Uptime", "").split()
    if up and up[0].isdigit():
        out["uptime"] = format_duration(int(up[0]))
    return out


@app.get("/api/playit")
def api_playit():
    return jsonify({"status": playit_status(), "log": _tail(PLAYIT_LOG)})


@app.post("/api/playit/start")
def api_playit_start():
    return jsonify(_playit_run("start"))


@app.post("/api/playit/stop")
def api_playit_stop():
    return jsonify(_playit_run("stop"))


@app.post("/api/start-all")
def api_start_all():
    """Start everything: playit tunnel, Discord bot, and the last-used server."""
    out = {"playit": "started" if _playit_run("start").get("ok") else "unavailable"}
    with LOCK:
        if BOT and BOT.alive():
            out["bot"] = "already running"
        elif (BOT_DIR / "bot.py").is_file():
            _launch_bot()
            out["bot"] = "started"
        else:
            out["bot"] = "bot.py not found"

        if SERVER and SERVER.alive():
            out["server"] = f"{SERVER_META.get('name')} already running"
        else:
            name = _load_state().get("last_server")
            match = next((s for s in scan_servers(SERVERS_ROOT) if s["name"] == name), None) if name else None
            if match and match["scripts"]:
                script = _load_state().get("last_script")
                if script not in match["scripts"]:
                    script = match["scripts"][0]
                _launch_server(match, script)
                out["server"] = f"started {name}"
            elif name:
                out["server"] = f"last server '{name}' not found"
            else:
                out["server"] = "no server — pick one on the MC Server tab"
    return jsonify({"ok": True, "result": out})


@app.post("/api/stop-all")
def api_stop_all():
    """Stop everything: MC server (gracefully), Discord bot, and playit tunnel."""
    if SERVER and SERVER.alive():
        SERVER.stopping = True  # immediate UI feedback; worker does the real stop

    def worker():
        for fn in (_kill_existing_servers, _kill_existing_bots,
                   lambda: _playit_run("stop")):
            try:
                fn()
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


def _ensure_playit():
    """Best-effort: bring the tunnel up on launch (no-op if already running)."""
    if PLAYIT_EXE.is_file():
        _playit_run("start")


def _adopt_running():
    """On reopen, reconnect to an already-running server/bot so the dashboard
    reflects and can control them. Adopted processes have no console/stdin."""
    global SERVER, SERVER_META, BOT
    if not (SERVER and SERVER.alive()):
        by_folder = {}
        for pr in psutil.process_iter(["name", "cwd", "create_time"]):
            try:
                if _is_server_proc(pr.info["name"] or "", pr.info["cwd"] or "", str(SERVERS_ROOT)):
                    by_folder.setdefault(Path(pr.info["cwd"]), []).append(pr)
            except psutil.Error:
                continue
        if by_folder:
            folder = max(by_folder,  # the most-recently-started server folder
                         key=lambda f: max(p.info["create_time"] for p in by_folder[f]))
            procs = by_folder[folder]
            anchor = next((p for p in procs if (p.info["name"] or "").lower() == "cmd.exe"), procs[0])
            props = read_properties(folder / "server.properties")
            port = int(props.get("server-port") or 25565)
            SERVER = AdoptedProc(anchor.pid, folder.name)
            SERVER_META = {"name": folder.name, "port": port, "folder": str(folder),
                           "query": props.get("enable-query", "").lower() == "true",
                           "query_port": int(props.get("query.port") or port)}
    if not (BOT and BOT.alive()):
        best = None
        for pr in psutil.process_iter(["name", "cmdline"]):
            try:
                if not _is_bot_cmdline(pr.info["cmdline"]):
                    continue
                try:
                    if pr.cwd().lower() != str(BOT_DIR).lower():
                        continue
                except (psutil.Error, OSError):
                    pass
                if best is None or (pr.info["name"] or "").lower() == "python.exe":
                    best = pr  # prefer the real interpreter over the py.exe launcher
            except psutil.Error:
                continue
        if best:
            BOT = AdoptedProc(best.pid, "discord-bot")
    # playit is a Windows service; playit_status() already reflects it.


# ---------- entrypoints ----------

def _serve():
    app.run(host=CONFIG["host"], port=CONFIG["port"], threaded=True, use_reloader=False)


def run_web():
    url = f"http://{CONFIG['host']}:{CONFIG['port']}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    _serve()


def run_desktop():
    import webview
    url = f"http://{CONFIG['host']}:{CONFIG['port']}"
    threading.Thread(target=_serve, daemon=True).start()
    for _ in range(100):  # wait up to ~10s for Flask to accept connections
        if is_server_up(CONFIG["host"], CONFIG["port"], timeout=0.2):
            break
        time.sleep(0.1)
    webview.create_window("Minecraft Dashboard", url, width=1320, height=900,
                          confirm_close=True)
    webview.start()


if __name__ == "__main__":
    _adopt_running()  # reconnect to any server/bot already running
    threading.Thread(target=_ensure_playit, daemon=True).start()
    if "--web" in sys.argv:
        run_web()
    else:
        try:
            run_desktop()
        except ImportError:
            print("pywebview not installed; opening in browser instead. "
                  "Install it with: py -m pip install pywebview")
            run_web()
