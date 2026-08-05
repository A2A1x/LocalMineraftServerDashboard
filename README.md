# Local Minecraft Server Dashboard

A localhost web dashboard to launch one of your Minecraft servers, watch its live
status (players, uptime, version, MOTD, CPU/RAM, console log), send console commands,
and start/stop the [Discord bot](../MinecraftServerDiscordBot) with adjustable settings.

## Setup

Requires Python (the `py` launcher) and Java on PATH for the servers.

```bash
run.bat
```

That installs deps and opens <http://127.0.0.1:8765>. The dashboard binds to
`127.0.0.1` only, so it is not reachable from the network.

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

## Test

```bash
py test_app.py
```
