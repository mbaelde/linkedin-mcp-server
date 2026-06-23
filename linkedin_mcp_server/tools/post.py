"""
LinkedIn post/content search tool.

Performs LinkedIn's global content search (the "Posts" results tab) using
innerText extraction, so informal "we're hiring" / "Buscamos ..." posts can
be found before a formal job listing is published. Mirrors search_people:
build a /search/results/content/ URL, scroll to load results, and return the
raw innerText for the LLM to parse, plus post-permalink references.
"""

import logging
from datetime import date
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    FilterValidationError,
    rate_limited_section_error,
)

logger = logging.getLogger(__name__)


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post/content-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
    )
    @tool_errors
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
    ) -> dict[str, Any]:
        """
        Search LinkedIn posts/content globally by keyword (the "Posts" tab).

        Use this to catch informal hiring posts ("we're hiring", "Buscamos
        ...", "estamos contratando", "join our team") that often appear before
        a formal job listing exists. This is global content search, distinct
        from get_feed (your own home feed) and get_company_posts (one
        company's page).

        Args:
            keywords: Search keywords (e.g., "Buscamos Unity", "AI automation hiring")
            ctx: FastMCP context for progress reporting
            date_posted: Optional recency filter. One of "past-24h",
                "past-week", "past-month"; the "past_24_hours" / "past_week" /
                "past_month" spellings used by search_jobs are accepted too.
                Omit for any time.
            max_pages: Scroll depth as result "pages" of ~5 scrolls each
                (1-10, default 3). Content search is an infinite scroll, so
                this caps how far the page is scrolled rather than fetching
                discrete pages.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional
            references (post authors, companies, linked jobs, and kind
            "feed_post" permalinks read from the page's payload responses —
            /feed/update/<urn>/ or /posts/<slug>, both valid permalinks) and
            section_errors. The DOM carries no per-post permalink anchors;
            captured permalinks are not aligned to result order. The LLM
            should parse the raw text to extract each post's author,
            headline/role, company, body, posted date, and reaction/comment
            counts.
        """
        extractor = await get_ready_extractor(ctx, tool_name="search_posts")
        logger.info(
            "Searching posts: keywords='%s', date_posted='%s', max_pages=%d",
            keywords,
            date_posted,
            max_pages,
        )

        await ctx.report_progress(progress=0, total=100, message="Starting post search")

        try:
            result = await extractor.search_posts(
                keywords,
                date_posted=date_posted,
                max_pages=max_pages,
            )
        except FilterValidationError as e:
            # Validation messages carry actionable detail; surface them as
            # ToolError so mask_error_details doesn't reduce them to a
            # generic "Error calling tool 'search_posts'".
            raise ToolError(str(e)) from e

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Posts Engagement",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "scraping"},
    )
    @tool_errors
    async def get_posts_engagement(
        linkedin_username: str,
        ctx: Context,
        num_posts: Annotated[int | None, Field(ge=1, le=20)] = None,
        since: date | None = None,
        until: date | None = None,
        post_url: str | None = None,
    ) -> dict[str, Any]:
        """
        List who reacted to, commented on and reposted a person's posts.

        Reads the person's posts activity page and, for each selected post,
        opens the reactions and reposts lists and expands the comments.
        Select either the latest num_posts posts, the posts published between
        since and until, or the single post at post_url.
        Only the counters are clicked, never the like/comment/repost actions.
        Each post costs several page interactions, so keep num_posts small.

        Args:
            linkedin_username: Profile slug (e.g. "albanpeltier")
            ctx: FastMCP context for progress reporting
            num_posts: Maximum number of posts to read (1-20). Defaults to 3,
                or to 20 when since/until is given.
            since: First publication day to include (YYYY-MM-DD, UTC)
            until: Last publication day to include (YYYY-MM-DD, UTC)
            post_url: Permalink of one post by this person
                (https://www.linkedin.com/feed/update/urn:li:activity:.../ or
                a /posts/...-activity-<id>-... URL); overrides the other filters

        Returns:
            Dict with url, sections (engagement -> text) and optional
            section_errors. The text has one "=== POST <permalink> ===" block
            per post ("posted: <UTC time>", start of the post text, then
            REACTIONS, COMMENTS and REPOSTS lists). Each list line is "name | profile url | detail";
            reactor detail is the headline, comment detail is
            "comment:"/"reply:" followed by the author line and text. Reaction
            types are not reported, and collapsed reply threads stay collapsed.
        """
        if num_posts is None:
            num_posts = 20 if since or until else 3
        extractor = await get_ready_extractor(ctx, tool_name="get_posts_engagement")
        logger.info(
            "Reading posts engagement: username=%s, num_posts=%d",
            linkedin_username,
            num_posts,
        )
        await ctx.report_progress(
            progress=0, total=100, message="Reading posts engagement"
        )

        extracted = await extractor.get_posts_engagement(
            linkedin_username,
            num_posts=num_posts,
            since=since,
            until=until,
            post_url=post_url,
        )

        url = f"https://www.linkedin.com/in/{linkedin_username}/recent-activity/shares/"
        sections: dict[str, str] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["engagement"] = extracted.text
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["engagement"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["engagement"] = extracted.error

        await ctx.report_progress(progress=100, total=100, message="Complete")

        result: dict[str, Any] = {"url": url, "sections": sections}
        if section_errors:
            result["section_errors"] = section_errors
        return result
