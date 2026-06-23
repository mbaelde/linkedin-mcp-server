---
name: linkedin-post-engagement
description: Report who reacted to, commented on and reposted a LinkedIn person's posts (one post, the latest N posts, or the posts published between two dates), for contest tracking or engagement reviews. Use when the user asks for LinkedIn post stats, likes, comments, reposts or "who engaged" on someone's posts.
---

# LinkedIn post engagement

Answer with the people behind each post's reactions, comments and reposts,
using the `get_posts_engagement` tool of the LinkedIn MCP server. Reply in the
user's language.

## 1. Work out the request

- **Whose posts**: a profile slug, the part after `linkedin.com/in/` (for
  `https://www.linkedin.com/in/albanpeltier/` it is `albanpeltier`). If the
  user gives only a name and no slug or URL, ask for the profile URL. Do not
  guess.
- **Which posts**, exactly one of:
  - one post: the user pastes a post link. Pass it as `post_url`
    (`/feed/update/urn:li:activity:...` and `/posts/...-activity-<id>-...`
    links both work);
  - the latest N posts: `num_posts=N` (1 to 20, default 3);
  - a period: `since` and/or `until` as `YYYY-MM-DD`, both days included.
    Convert relative periods ("last week", "en septembre") to dates from
    today's date and state the dates you used. Pass `num_posts` only if the
    user caps the count (the tool reads up to 20 posts in a period).

## 2. Call the tool

`get_posts_engagement(linkedin_username=..., post_url=... | num_posts=... | since=..., until=...)`

Each post takes about 30 seconds (it opens the reaction and repost lists and
expands the comments), so tell the user to expect a wait when several posts are
requested. Never call like, comment, repost or message tools: this is read only.

## 3. Read the result

`sections.engagement` has one block per post:

```
=== POST <permalink> ===
posted: <YYYY-MM-DD HH:MM UTC>
<start of the post text>
--- REACTIONS (n) ---
name | profile url | headline
--- COMMENTS (n) ---
name | profile url | comment: <author line and text>   (or reply: ...)
--- REPOSTS (n) ---
name | profile url
```

- `(unavailable)` instead of a count means that list could not be opened; say
  so for that post and suggest retrying it alone with its `post_url`.
- `No matching post found.` means no post of that person matches the link or
  the period.
- Reaction counts include company pages (profile URL `/company/...`).
- Some reactor profile URLs are opaque ids (`/in/ACoAA...`): they open the
  right profile, but match people by name when comparing lists.
- The type of reaction (like, celebrate...) is not reported, and replies
  hidden in collapsed threads are not counted.
- `section_errors` or an authentication error: report it as is and stop; the
  user may need to log in to the LinkedIn MCP again.

## 4. Answer

For each post, oldest first: date, the first words of the post, its link, then
the counts (reactions, comments, replies, reposts) and the three lists of
names. Keep comment text short.

If the user is tracking a contest or wants a summary across posts, add a table
with one row per person and their reaction, comment and repost counts across
the posts, sorted by total. Write the post author's own reactions and replies
as a separate line rather than mixing them in.

Offer a CSV (`post_date,post_url,action,name,profile_url,detail`, one row per
reaction, comment or repost) when the user wants to paste the result in a
spreadsheet.
