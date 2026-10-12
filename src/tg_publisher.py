"""Final posts ko destination me ek-ek karke publish karna (order me, har post confirm hone ke baad agla).

Duplicate se bachav: content publish se pehle DB me save; 'sending' state me crash ho ya send ka result
ambiguous ho to destination me recent messages me wahi text dhoondh ke adopt karte hain, tabhi dobara bhejte hain.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol

from .db_stores import PostStore
from .diskwala_uploader import flood_seconds, is_fatal, is_transient


class PublishBlocked(Exception):
    """Post publish nahi ho paya -> ZIP pause. Aage ke posts nahi bheje."""


class Destination(Protocol):
    async def send(self, text: str) -> int: ...
    async def find_recent(self, text: str, since_iso: str) -> int | None: ...


class TelethonDestination:
    def __init__(self, client, entity, link_preview: bool = False):
        self.client, self.entity, self.link_preview = client, entity, link_preview

    async def send(self, text: str) -> int:
        return (await self.client.send_message(self.entity, text, link_preview=self.link_preview)).id

    async def find_recent(self, text: str, since_iso: str) -> int | None:
        since = datetime.fromisoformat(since_iso) - timedelta(seconds=120)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        want = text.strip()
        for m in await self.client.get_messages(self.entity, limit=30):
            if m.out and m.date and m.date >= since and (m.raw_text or "").strip() == want:
                return m.id
        return None


@dataclass
class PublishPolicy:
    max_attempts: int = 5
    base_delay: float = 5.0
    max_delay: float = 300.0
    max_flood_wait: int = 900
    gap_between_posts: float = 3.0


class PostPublisher:
    def __init__(self, dest: Destination, posts: PostStore, policy: PublishPolicy | None = None, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rng: Callable[[], float] = random.random):
        self.dest, self.posts = dest, posts
        self.policy = policy or PublishPolicy()
        self.sleep, self.rng = sleep, rng

    def _delay(self, attempt: int) -> float:
        ceiling = min(self.policy.max_delay, self.policy.base_delay * 2 ** (attempt - 1))
        return ceiling / 2 + self.rng() * ceiling / 2  # equal jitter

    async def publish_all(self, archive_id: str) -> int:
        """Pending posts order me bhejo. Returns published count (is run me + pehle ke)."""
        posts = self.posts.list(archive_id)
        for i, post in enumerate(posts):
            if post["status"] == "published":
                continue
            await self._publish_one(archive_id, post)
            if i < len(posts) - 1:
                await self.sleep(self.policy.gap_between_posts)
        return sum(p["status"] == "published" for p in self.posts.list(archive_id))

    async def _adopt(self, archive_id: str, post: dict, since_iso: str | None) -> bool:
        if not since_iso:
            return False
        try:
            found = await self.dest.find_recent(post["content"], since_iso)
        except Exception:  # noqa: BLE001 - reconcile fail => abhi adopt nahi; caller decide karega
            return False
        if found:
            self.posts.mark_published(archive_id, post["seq"], found)
        return bool(found)

    async def _publish_one(self, archive_id: str, post: dict) -> None:
        seq, name = post["seq"], f"post {post['seq']}"
        if post["status"] == "sending" and await self._adopt(archive_id, post, post.get("sending_started_at")):
            return  # pichli run me bhej chuke the, bas record nahi hua tha
        floods = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            self.posts.mark_sending(archive_id, seq)
            started = self.posts.list(archive_id)[seq - 1]["sending_started_at"]
            try:
                mid = await self.dest.send(post["content"])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                secs = flood_seconds(e)
                if secs is not None:  # FloodWait: message gaya hi nahi
                    floods += 1
                    if secs > self.policy.max_flood_wait or floods > 5:
                        self.posts.mark_failed(archive_id, seq, f"FloodWait {secs}s")
                        raise PublishBlocked(f"{name}: Telegram FloodWait {secs}s - baad me resume karo.") from e
                    await self.sleep(secs + 1)
                    continue
                if is_fatal(e) or not is_transient(e):
                    self.posts.mark_failed(archive_id, seq, f"{type(e).__name__}: {e}")
                    raise PublishBlocked(f"{name}: {type(e).__name__}: {e}") from e
                if await self._adopt(archive_id, post, started):  # ambiguous: shayad gaya tha
                    return
                self.posts.mark_failed(archive_id, seq, f"transient: {type(e).__name__}")
                if attempt == self.policy.max_attempts:
                    raise PublishBlocked(f"{name}: {attempt} koshish ke baad bhi fail ({type(e).__name__}).") from e
                await self.sleep(self._delay(attempt))
                continue
            self.posts.mark_published(archive_id, seq, mid)
            return
        raise PublishBlocked(f"{name}: FloodWait retries khatam.")
