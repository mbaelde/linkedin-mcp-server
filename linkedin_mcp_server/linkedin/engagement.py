"""Per-post engagement (reactors, commenters, reposters) from a profile's activity page.

LinkedIn serves ``/in/<user>/recent-activity/shares/`` and
``/company/<slug>/posts/`` with the classic (Ember)
feed markup, whose per-post social counters are standalone buttons that open
read-only lists. The SDUI pages (``/feed/update/...``, ``/recent-activity/all/``)
only expose the repost count inside the "Repost" action button, so they are not
used here. Only counter buttons are clicked, never the like/comment/repost
actions.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from patchright.async_api import Locator
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

# Classic feed markup. Class names are the only locale-independent handle on
# which counter is which (the counters are plain buttons with localized labels).
_ACTIVITY = "[data-urn^='urn:li:activity:']"
# Top-level cards only: a repost with commentary nests the quoted post's card.
_CARD = f"{_ACTIVITY}:not({_ACTIVITY} *)"
_REACTIONS_COUNTER = "button[data-reaction-details]"
_COMMENTS_COUNTER = ".social-details-social-counts__comments button"
_COUNTER_ITEM = ".social-details-social-counts__item"
_DIALOG = "[role=dialog]"
_DIALOG_DISMISS = "button.artdeco-modal__dismiss"
_REACTOR_ROW = "li.social-details-reactors-tab-body-list-item"
_REACTORS_MORE = ".scaffold-finite-scroll button"
_COMMENT = "article.comments-comment-entity"
_COMMENTS_MORE = "button[class*='comments-comments-list__load-more-comments-button']"
_ACTIVITY_HEADER = ".update-components-header"
_REPOSTER_LINK = ".update-components-header__text-view a[href]"

_MAX_LOAD_MORE = 20
_MAX_SCROLLS = 30
# A permalink's share/ugcPost id and the activity id on the card are minted
# a few minutes apart, so a specific post is matched by nearest timestamp.
_MATCH_WINDOW_MS = 3_600_000
_MAX_POST_CHARS = 300

# Row extractors run in the page; each returns [[name, url, detail], ...].
_REACTORS_JS = """rows => rows.map(r => {
    const a = r.querySelector('a[href]');
    const lines = r.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    return [lines[0] || '', a ? a.href.split('?')[0] : '', lines.slice(1).join(' ')];
})"""
_COMMENTS_JS = """rows => rows.map(r => {
    const a = r.querySelector('a[href*="/in/"], a[href*="/company/"]');
    const lines = r.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    const reply = /--reply/.test(r.className) ? 'reply' : 'comment';
    return [lines[0] || '', a ? a.href.split('?')[0] : '', reply + ': ' + lines.slice(1).join(' ')];
})"""
_REPOSTERS_JS = """links => links.filter(a => a.innerText.trim()).map(a => [
    a.innerText.trim(), a.href.split('?')[0], ''
])"""


def post_time_ms(urn_or_url: str) -> int | None:
    """Creation time (ms since epoch) encoded in a LinkedIn post id's top 41 bits."""
    match = re.search(r"\d{19}", urn_or_url)
    return int(match.group()) >> 22 if match else None


def _day_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def select_posts(
    urns: list[str],
    num_posts: int,
    since: date | None = None,
    until: date | None = None,
    post_url: str | None = None,
) -> list[int]:
    """Indexes of the cards to read, in feed order."""
    times = [post_time_ms(u) or 0 for u in urns]
    if post_url:
        target = post_time_ms(post_url)
        if target is None or not times:
            return []
        best = min(range(len(times)), key=lambda i: abs(times[i] - target))
        return [best] if abs(times[best] - target) <= _MATCH_WINDOW_MS else []
    low = _day_ms(since) if since else 0
    high = _day_ms(until + timedelta(days=1)) if until else 1 << 62
    return [i for i, ms in enumerate(times) if low <= ms < high][:num_posts]


def _format_rows(title: str, rows: list[list[str]]) -> str:
    seen: set[tuple[str, str, str]] = set()
    lines = []
    for name, url, detail in rows:
        if (name, url, detail) in seen:
            continue
        seen.add((name, url, detail))
        lines.append(" | ".join(part for part in (name, url, detail) if part))
    return f"--- {title} ({len(lines)}) ---\n" + "\n".join(lines)


class PostEngagementReader:
    """Read who reacted to, commented on and reposted a profile's latest posts."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def get_posts_engagement(
        self,
        username: str,
        num_posts: int = 3,
        since: date | None = None,
        until: date | None = None,
        post_url: str | None = None,
    ) -> ExtractedSection:
        """Return one text block per post with its reactors, commenters and reposters.

        Posts are the latest ``num_posts``, optionally restricted to the
        ``since``..``until`` days (UTC, inclusive), or the single post at
        ``post_url``.
        """
        url = (
            f"https://www.linkedin.com/{username}/posts/"
            if username.startswith("company/")
            else f"https://www.linkedin.com/in/{username}/recent-activity/shares/"
        )
        try:
            return await self._read(url, num_posts, since, until, post_url)
        except LinkedInOperationError:
            raise
        except Exception as e:
            logger.warning("Post engagement extraction failed for %s: %s", url, e)
            return ExtractedSection(
                text="",
                references=[],
                error=build_issue_diagnostics(
                    e,
                    context="get_posts_engagement",
                    target_url=url,
                    section_name="engagement",
                ),
            )

    async def _read(
        self,
        url: str,
        num_posts: int,
        since: date | None,
        until: date | None,
        post_url: str | None,
    ) -> ExtractedSection:
        page = self._session.page
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        try:
            await page.wait_for_selector(_CARD, timeout=15000)
        except PlaywrightTimeoutError:
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])

        # Scroll until the feed reaches back past the oldest wanted post.
        oldest_ms = post_time_ms(post_url) if post_url else None
        if since:
            oldest_ms = _day_ms(since)
        if oldest_ms is not None:
            oldest_ms -= _MATCH_WINDOW_MS
        cards = page.locator(_CARD)
        last = -1
        for _ in range(_MAX_SCROLLS):
            urns = await self._urns(cards)
            if len(urns) == last:
                break
            last = len(urns)
            if oldest_ms is None:
                if len(urns) >= num_posts:
                    break
            elif urns and (post_time_ms(urns[-1]) or 0) < oldest_ms:
                break
            await page.mouse.wheel(0, 3000)
            await self._session.delay(2.0)

        urns = await self._urns(cards)
        picked = select_posts(urns, num_posts, since, until, post_url)
        # Locate by URN: the feed re-renders cards, so positions are not stable.
        blocks = [
            await self._read_card(page.locator(f"{_CARD}[data-urn='{urns[i]}']").first)
            for i in picked
        ]
        return ExtractedSection(
            text="\n\n".join(blocks) or "No matching post found.", references=[]
        )

    @staticmethod
    async def _urns(cards: Locator) -> list[str]:
        """URNs of the profile's own posts, newest first.

        LinkedIn may redirect ``/shares/`` to ``/all/``, which also lists
        "<name> liked/commented/reposted this" cards, and a company's
        ``/posts/`` page lists its reposts; those carry a header and their
        counters belong to someone else's post, so they are skipped.
        """
        return await cards.evaluate_all(
            f"cs => cs.filter(c => !c.querySelector('{_ACTIVITY_HEADER}'))"
            ".map(c => c.dataset.urn)"
        )

    async def _read_card(self, card: Locator) -> str:
        urn = await card.get_attribute("data-urn") or ""
        body = (await card.inner_text()).strip()[:_MAX_POST_CHARS]
        ms = post_time_ms(urn)
        posted = (
            datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC")
            if ms
            else "unknown"
        )
        parts = [
            f"=== POST https://www.linkedin.com/feed/update/{urn}/ ===",
            f"posted: {posted}",
            body,
        ]
        for title, read in (
            ("REACTIONS", self._reactors),
            ("COMMENTS", self._comments),
            ("REPOSTS", self._reposters),
        ):
            try:
                parts.append(await read(card))
            except PlaywrightTimeoutError as e:
                # One list that will not open must not lose the other posts.
                logger.warning("%s list unavailable for %s: %s", title, urn, e)
                await self._close_dialog()
                parts.append(f"--- {title} (unavailable) ---")
        return "\n".join(parts)

    async def _open_dialog(self, button: Locator) -> Locator:
        # Dispatched rather than clicked: sticky bars and expanded comments of
        # the previous card often cover the counter and stall a real click.
        await button.dispatch_event("click")
        dialog = self._session.page.locator(_DIALOG).last
        await dialog.wait_for(timeout=10000)
        await self._session.delay(1.5)
        return dialog

    async def _close_dialog(self) -> None:
        page = self._session.page
        dialogs = page.locator(_DIALOG)
        # Escape alone is swallowed when an expanded comment box holds focus,
        # leaving the modal open for the next post's reads.
        dismiss = dialogs.locator(_DIALOG_DISMISS)
        if await dismiss.count():
            await dismiss.last.dispatch_event("click")
        else:
            await page.keyboard.press("Escape")
        try:
            await dialogs.last.wait_for(state="detached", timeout=5000)
        except PlaywrightTimeoutError:
            logger.warning("Engagement dialog did not close")

    async def _scroll_dialog(self, dialog: Locator, row: str) -> None:
        """Scroll the modal body (and click its load-more button) until rows stop growing."""
        rows = dialog.locator(row)
        last = -1
        for _ in range(_MAX_LOAD_MORE):
            count = await rows.count()
            if count == last:
                break
            last = count
            await dialog.evaluate(
                "d => d.querySelectorAll('.artdeco-modal__content').forEach(e => e.scrollTop = e.scrollHeight)"
            )
            more = dialog.locator(_REACTORS_MORE)
            if await more.count() and await more.first.is_visible():
                await more.first.click()
            await self._session.delay(1.5)

    async def _reactors(self, card: Locator) -> str:
        button = card.locator(_REACTIONS_COUNTER)
        if not await button.count():
            return _format_rows("REACTIONS", [])
        dialog = await self._open_dialog(button.first)
        try:
            await self._scroll_dialog(dialog, _REACTOR_ROW)
            rows = await dialog.locator(_REACTOR_ROW).evaluate_all(_REACTORS_JS)
        finally:
            await self._close_dialog()
        return _format_rows("REACTIONS", rows)

    async def _comments(self, card: Locator) -> str:
        button = card.locator(_COMMENTS_COUNTER)
        if not await button.count():
            return _format_rows("COMMENTS", [])
        await button.first.dispatch_event("click")
        await self._session.delay(2.0)
        # ponytail: top-level "load more" only; collapsed reply threads stay collapsed.
        for _ in range(_MAX_LOAD_MORE):
            more = card.locator(_COMMENTS_MORE)
            if not await more.count():
                break
            await more.first.dispatch_event("click")
            await self._session.delay(1.5)
        rows = await card.locator(_COMMENT).evaluate_all(_COMMENTS_JS)
        return _format_rows("COMMENTS", rows)

    async def _reposters(self, card: Locator) -> str:
        # The repost counter is the counter item that is neither reactions nor comments.
        button = card.locator(
            f"{_COUNTER_ITEM}:not(.social-details-social-counts__reactions)"
            ":not(.social-details-social-counts__comments) button[aria-label]"
        )
        if not await button.count():
            return _format_rows("REPOSTS", [])
        dialog = await self._open_dialog(button.first)
        try:
            await self._scroll_dialog(dialog, _REPOSTER_LINK)
            rows: list[Any] = await dialog.locator(_REPOSTER_LINK).evaluate_all(
                _REPOSTERS_JS
            )
        finally:
            await self._close_dialog()
        return _format_rows("REPOSTS", rows)
