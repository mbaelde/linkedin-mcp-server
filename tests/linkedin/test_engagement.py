from datetime import UTC, date, datetime

from linkedin_mcp_server.linkedin.engagement import (
    _format_rows,
    post_time_ms,
    select_posts,
)


def test_format_rows_dedupes_and_skips_empty_parts():
    rows = [
        ["Ada", "https://www.linkedin.com/in/ada/", "Engineer"],
        ["Ada", "https://www.linkedin.com/in/ada/", "Engineer"],
        ["Acme", "https://www.linkedin.com/company/acme/", ""],
    ]
    assert _format_rows("REACTIONS", rows) == (
        "--- REACTIONS (2) ---\n"
        "Ada | https://www.linkedin.com/in/ada/ | Engineer\n"
        "Acme | https://www.linkedin.com/company/acme/"
    )


def test_format_rows_empty():
    assert _format_rows("REPOSTS", []) == "--- REPOSTS (0) ---\n"


def _urn(day: int, hour: int = 12) -> str:
    ms = int(datetime(2026, 10, day, hour, tzinfo=UTC).timestamp() * 1000)
    return f"urn:li:activity:{ms << 22}"


def test_post_time_decodes_the_id_timestamp():
    # Alban Peltier's post published 2026-10-01 12:51 UTC.
    ms = post_time_ms("urn:li:activity:7511407427477585920")
    assert ms is not None
    assert datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M") == (
        "2026-10-01 12:51"
    )
    assert post_time_ms("no id here") is None


def test_select_posts_by_count_dates_and_url():
    urns = [_urn(5), _urn(3), _urn(1), _urn(1, 1)]  # newest first, as on the feed
    assert select_posts(urns, 2) == [0, 1]
    assert select_posts(urns, 20, since=date(2026, 10, 1), until=date(2026, 10, 3)) == [
        1,
        2,
        3,
    ]
    assert select_posts(urns, 20, since=date(2026, 10, 4)) == [0]
    # A permalink id minted a few minutes after the card's activity id.
    near = _urn(3).replace("urn:li:activity:", "")
    later = str(int(near) + (240_000 << 22))
    assert select_posts(
        urns, 3, post_url=f"https://www.linkedin.com/feed/update/urn:li:share:{later}/"
    ) == [1]
    assert (
        select_posts(
            urns,
            3,
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000000/",
        )
        == []
    )
