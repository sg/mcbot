"""In-process fan-out for live web feeds (no third-party deps).

The bot's event handlers call `publish()`; each connected WebSocket client
holds a bounded queue obtained via `subscribe()`. Slow clients drop their
oldest queued item rather than block the bot's event loop. Importing this
module pulls in no web dependencies, so mcbot can hold broadcasters even
when the web UI is disabled.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Broadcaster:
    def __init__(self, maxsize: int = 200):
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def has_subscribers(self) -> bool:
        return bool(self._subs)

    def publish(self, item: Any) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # Drop the oldest item to make room for the newest.
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except Exception:
                    pass
