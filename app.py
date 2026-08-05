import json
import os
import re
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

class Proc:
    """A launched subprocess with a captured, size-capped console log."""

    def __init__(self, popen: subprocess.Popen, label: str):
        self.popen = popen
        self.label = label
        self.started_at = time.time()
        self.log = deque(maxlen=500)
        self.stopping = False
        self._pcache = {}  # pid -> psutil.Process, kept across polls for cpu deltas
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

    def kill_tree(self):
        try:
            parent = psutil.Process(self.popen.pid)
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
            parent = psutil.Process(self.popen.pid)
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


LOCK = threading.Lock()
SERVER: Proc | None = None
SERVER_META: dict = {}  # {name, port}
BOT: Proc | None = None


def _graceful_stop(proc: Proc, timeout: float = 90.0):
    proc.stopping = True
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


@app.post("/api/server/start")
def api_server_start():
    global SERVER, SERVER_META
    with LOCK:
        data = request.get_json(force=True)
        name, script = data.get("name"), data.get("script")
        match = next((s for s in scan_servers(SERVERS_ROOT) if s["name"] == name), None)
        if not match:
            return jsonify({"error": "Unknown server."}), 404
        if script not in match["scripts"]:
            return jsonify({"error": "Unknown start script."}), 400
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
        SERVER = Proc(popen, name)
        SERVER_META = {
            "name": name,
            "port": match["port"],
            # Query (GS4) gives the full player list; the status sample is
            # capped/anonymized. Only used when the server enables it.
            "query": props.get("enable-query", "").lower() == "true",
            "query_port": int(props.get("query.port") or match["port"]),
        }
        return jsonify({"ok": True, "replaced": killed})


@app.post("/api/server/command")
def api_server_command():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    cmd = (request.get_json(force=True).get("cmd") or "").strip()
    if not cmd:
        return jsonify({"error": "Empty command."}), 400
    SERVER.send(cmd)
    return jsonify({"ok": True})


@app.post("/api/server/stop")
def api_server_stop():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    threading.Thread(target=_graceful_stop, args=(SERVER,), daemon=True).start()
    return jsonify({"ok": True})


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
        "log": list(SERVER.log),
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


@app.post("/api/bot/start")
def api_bot_start():
    global BOT
    with LOCK:
        if not (BOT_DIR / "bot.py").is_file():
            return jsonify({"error": f"bot.py not found in {BOT_DIR}"}), 404
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
        return jsonify({"ok": True, "replaced": killed})


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


def _ensure_playit():
    """Best-effort: bring the tunnel up on launch (no-op if already running)."""
    if PLAYIT_EXE.is_file():
        _playit_run("start")


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
