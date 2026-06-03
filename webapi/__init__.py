"""Web admin UI + REST/WebSocket API for mcbot.

Runs in-process in the bot's asyncio loop (see MCBot._start_web), so it has
direct, lock-safe access to the live meshcore connection, the SQLite DB, the
packet decoder, and the event stream. Optional — only active when the
[web] config section has enabled=true.
"""
