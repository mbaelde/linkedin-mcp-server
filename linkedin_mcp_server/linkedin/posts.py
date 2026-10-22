"""Content-search workflow behind the LinkedIn "Posts" tab."""

from __future__ import annotations

import re
from typing import Any

from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    FilterValidationError,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.search_urls import build_content_search_url

# Content search is an infinite scroll with no ``&start=`` pagination, so
# ``max_pages`` caps scroll depth instead of fetching discrete pages. One
# nominal "page" is this many scrolls.
_CONTENT_SCROLLS_PER_REQUESTED_PAGE = 5


class PostSearch:
    """Own the one workflow whose subject is LinkedIn post content.

    The search is a single capture, so the section reader is the only
    collaborator it takes: there is no walk to pace and no page to navigate
    itself.
    """

    def __init__(self, capture: SectionCapture):
        self._capture = capture

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Search LinkedIn posts/content and extract the results page.

        Reproduces the LinkedIn "Posts" content-search tab — the surface for
        catching informal "we're hiring" / "Buscamos ..." posts before a
        formal job listing exists.

        Args:
            keywords: Free-text query (e.g. "Buscamos Unity", "estamos contratando").
            date_posted: Optional recency filter, one of the keys of
                ``search_urls.CONTENT_DATE_POSTED_MAP``. Invalid values raise
                ``FilterValidationError`` (a ``ValueError`` subclass) rather
                than reaching LinkedIn, which would ignore them silently and
                return unfiltered results that look filtered.
            max_pages: Scroll depth, expressed in result "pages" of roughly
                ``_CONTENT_SCROLLS_PER_REQUESTED_PAGE`` scrolls each (default
                3). Content search is an infinite scroll with no per-page URL,
                so this caps how far the page is scrolled rather than fetching
                discrete ``&start=`` pages.

        Returns:
            {url, sections: {search_results: text}} plus optional ``references``
            (post authors, companies, linked jobs, and ``feed_post`` permalinks
            read from the payload responses) and ``section_errors``.
            Verified live: the results page renders no per-post permalink
            anchors in the DOM; the permalinks come from the JSON/document
            responses instead, in either ``/feed/update/<urn>/`` or
            ``/posts/<slug>`` form (both valid). The LLM should parse the raw
            text to extract each post's author, headline, body, date, and
            reaction counts.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(keywords, date_posted=date_posted)
        max_scrolls = max(1, max_pages) * _CONTENT_SCROLLS_PER_REQUESTED_PAGE
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(
                CaptureMode.SEARCH_RESULTS | CaptureMode.POST_PERMALINKS,
                max_scrolls,
            ),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = {
                "error_type": "rate_limit",
                "error_message": extracted.text,
            }
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def extract_post_analytics(self, activity_urn: str) -> dict[str, Any]:
        """Extract the analytics summary page of one of your own posts.

        Args:
            activity_urn: ``"urn:li:activity:<id>"`` or the bare numeric id,
                as exposed by references of kind ``"post_analytics"``.

        Returns:
            {url, sections: {post_analytics: text}} plus optional
            ``references`` and ``section_errors``.
        """
        if not re.fullmatch(r"(?:urn:li:activity:)?\d+", activity_urn):
            raise FilterValidationError(
                "activity_urn must be a numeric activity id, optionally prefixed "
                f'with "urn:li:activity:"; got {activity_urn!r}.'
            )
        if not activity_urn.startswith("urn:li:activity:"):
            activity_urn = f"urn:li:activity:{activity_urn}"

        url = f"https://www.linkedin.com/analytics/post-summary/{activity_urn}/"
        # The stats panel renders after the header chrome; ACTIVITY waits for
        # it. The page is short, so a few scrolls are enough.
        extracted = await self._capture.capture(
            url,
            section_name="post_analytics",
            plan=CapturePlan(CaptureMode.ACTIVITY, max_scrolls=2),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["post_analytics"] = extracted.text
            if extracted.references:
                references["post_analytics"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["post_analytics"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["post_analytics"] = extracted.error

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result
