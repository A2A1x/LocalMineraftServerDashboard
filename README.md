# Local Minecraft Server Dashboard

A native desktop app (Flask UI wrapped in a `pywebview` window) to launch one of your
Minecraft servers, watch its live status (players, uptime, version, MOTD, CPU/RAM,
console log), send console commands, and start/stop the
[Discord bot](../MinecraftServerDiscordBot) with adjustable settings.

## Run

Double-click the **Minecraft Dashboard** shortcut on your desktop (launches with no
console window), or from a terminal:

```bash
run.bat
```

`run.bat` installs deps and opens the app window. It runs a Flask server bound to
`127.0.0.1` only (not reachable from the network) and displays it in a native window
via the Edge WebView2 runtime built into Windows.

To open in your browser instead of a window (e.g. for debugging):

```bash
py app.py --web
```

## Config (`config.json`)

| Key | Meaning |
| --- | --- |
| `servers_root` | Folder that holds your server folders (each with a `server.properties` + a start `.bat`) |
| `bot_dir` | The Discord bot repo (used to launch `bot.py` and edit its `.env`) |
| `host` / `port` | Where the dashboard listens |

## How it works

- **Servers**: each subfolder of `servers_root` containing a `server.properties` is
  listed with its `.bat` files. Pick a script and Start. Only one server runs at a
  time. **Stop** sends `stop` and waits for a clean world-save before any force-kill.
- **Status**: reachability + `mcstatus` ping for players/version/MOTD, `psutil` for
  CPU/RAM of the server process tree. Console output streams into the log panel; the
  command box sends any command to the server.
- **Bot**: Save writes `CHANNEL_ID`, `MC_HOST`, `MC_PORT`, `POLL_INTERVAL`, `GUILD_ID`
  into the bot's `.env` (your `DISCORD_TOKEN` is preserved and never shown). Start/Stop
  runs `bot.py` as a subprocess; settings apply on next start.
- **Playit**: controls the [playit.gg](https://playit.gg) tunnel via its bundled CLI
  (`playit start` / `stop` / `status`, no admin needed) and shows a live tail of the
  daemon log (`C:\ProgramData\playit_gg\logs\playitd.log`) as its console. The app also
  runs `playit start` on launch, so the tunnel is up whenever the dashboard is open.

The UI has four tabs: **Overview** (at-a-glance tiles for everything), **MC Server**,
**Discord Bot**, and **Playit**.

## Test

```bash
py test_app.py
```
