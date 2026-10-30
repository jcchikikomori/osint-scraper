"""The Threads HTTP transport: user agents, the GraphQL session and its pages.

Threads' web app reads a profile's timeline over a Relay GraphQL endpoint.
The endpoint needs a ``doc_id`` -- the numeric id of the persisted query --
and Meta rotates it whenever a new bundle ships.  A hardcoded value therefore
breaks without warning.

The server-rendered profile page solves that: it embeds the preload manifest
the server itself used, which names the query, its ``doc_id`` and the exact
``variables`` for the current bundle.  The session is mined from that page at
run time, so a bundle rotation is picked up on the next run instead of
silently returning nothing.

The same page carries the ``LSD`` token that the GraphQL endpoint requires.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

THREADS_USER_AGENTS: list[str] = [
    ("Threads 330.0.0.29.111 (iPhone14,2; iOS 17_5; en_US; en; scale=3.00; 1170x2532; 452408278)"),
    ("Threads 329.0.0.29.108 (iPhone15,2; iOS 17_4_1; en_US; en; scale=3.00; 1179x2556; 451000000)"),
]
"""The mobile-app user agents Threads accepts.

A desktop agent receives an empty shell.  The mobile agent receives the
payload.  The second entry is used to retry an intermittently empty response.
"""

GRAPHQL_ENDPOINT = "https://www.threads.net/api/graphql"
"""The Relay endpoint the Threads web app posts its timeline query to."""

GRAPHQL_POST_UNAVAILABLE = 4686025
"""The server cannot resolve a quoted post.

This is what the *last* page of a timeline answers with, because the server
walks the timeline into a deleted post.  It means "stop", not "retry".
"""

GRAPHQL_PRIVATE_PROFILE = 1675030
"""The profile is private and cannot be read without a session for it."""

_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_PRELOADERS_KEY = '"expectedPreloaders":['
_PROFILE_QUERY_MARKER = "ProfileThreadsTab"


class ThreadsGraphQLError(Exception):
    """An error returned by the Threads GraphQL endpoint.

    Attributes:
        code: The numeric error code.
        summary: The short error summary.
        message: The human-readable message.
        path: The error path inside the query, when the server gave one.
    """

    def __init__(
        self,
        code: Any = None,
        summary: Any = None,
        message: Any = None,
        path: Any = None,
    ) -> None:
        """Store the error fields and build a readable message.

        Args:
            code: The numeric error code.
            summary: The short error summary.
            message: The human-readable message.
            path: The error path inside the query.
        """
        self.code = code
        self.summary = summary
        self.message = message
        self.path = path
        super().__init__(f"{code} {summary}: {message} path={path}")


@dataclass(frozen=True, slots=True)
class ThreadsGraphQLSession:
    """Everything needed to page a profile timeline over GraphQL.

    Attributes:
        lsd: The ``LSD`` token the endpoint requires.
        doc_id: The numeric id of the persisted timeline query.
        query_name: The friendly name of the query.
        variables: The variables for the query, exactly as the server sent
            them.  They carry the profile's numeric id and a long tail of
            feature flags; copying them verbatim keeps them correct across
            bundle rotations.
        user_id: The profile's numeric user id.
        html: The profile page the session was mined from, so that a caller
            falling back to the server-rendered posts need not fetch it twice.
    """

    lsd: str
    doc_id: str
    query_name: str
    variables: dict[str, Any]
    user_id: str
    html: str = ""


def jazoest(token: str) -> str:
    """Return the ``jazoest`` value the endpoint expects for ``token``.

    Args:
        token: The ``LSD`` token.

    Returns:
        The encoded value.
    """
    return f"2{len(token)}_{sum(ord(char) for char in token)}"


def scan_json_array(text: str, start: int) -> str | None:
    """Return the JSON array literal that begins at ``text[start]``.

    Bracket counting has to skip over string literals: the payload embeds
    JSON-encoded strings -- route paths, module names -- that contain literal
    ``[`` and ``]`` characters.  Counting them would cut the array short.

    Args:
        text: The text to scan.
        start: The index of the opening bracket.

    Returns:
        The array literal, or ``None`` when it is not closed.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_preloaders(html: str) -> list[dict[str, Any]]:
    """Return the ``expectedPreloaders`` array from a server-rendered page.

    Args:
        html: The page HTML.

    Returns:
        The preload entries, or an empty list when the page carries none.
    """
    index = html.find(_PRELOADERS_KEY)
    if index == -1:
        return []
    raw = scan_json_array(html, index + len(_PRELOADERS_KEY) - 1)
    if raw is None:
        return []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return loaded if isinstance(loaded, list) else []


def extract_lsd(html: str) -> str:
    """Return the ``LSD`` token embedded in a server-rendered page.

    Args:
        html: The page HTML.

    Returns:
        The token, or an empty string when the page carries none.
    """
    match = _LSD_RE.search(html)
    return match.group(1) if match else ""


def profile_url(username: str) -> str:
    """Return the profile page URL for ``username``."""
    return f"https://www.threads.net/@{username}"


def post_url(username: str, code: str) -> str:
    """Return the post page URL for ``username`` and ``code``."""
    return f"https://www.threads.net/@{username}/post/{code}"


def page_headers(user_agent: str, *, referer: str = "https://www.threads.net/") -> dict[str, str]:
    """Return the headers a Threads page request needs.

    The explicit ``Accept`` header is not optional: without it Threads answers
    with a large shell that carries no posts and no error.

    Args:
        user_agent: The user agent to present.
        referer: The referring page.

    Returns:
        The headers.
    """
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }


async def fetch_profile_html(
    client: httpx.AsyncClient,
    username: str,
    *,
    cookie_header: str = "",
    user_agent: str | None = None,
) -> str:
    """Fetch the server-rendered profile page.

    Args:
        client: The HTTP client to use.
        username: The Threads handle.
        cookie_header: The ``Cookie`` header to send.
        user_agent: The user agent to present. Defaults to the first mobile
            agent.

    Returns:
        The page HTML.
    """
    agent = user_agent or THREADS_USER_AGENTS[0]
    headers = page_headers(agent)
    if cookie_header:
        headers["Cookie"] = cookie_header

    response = await client.get(profile_url(username), headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response.text


def open_graphql_session_from_html(html: str) -> ThreadsGraphQLSession:
    """Mine a server-rendered profile page for everything GraphQL needs.

    One response carries the ``LSD`` token, the ``doc_id`` of the timeline
    query, the query's exact ``variables`` and the profile's numeric id.

    Args:
        html: The page HTML.

    Returns:
        The session.

    Raises:
        ThreadsGraphQLError: When the page carries no timeline preloader.
    """
    for preloader in extract_preloaders(html):
        query_name = preloader.get("queryName", "")
        if _PROFILE_QUERY_MARKER not in query_name:
            continue
        variables = preloader.get("variables")
        if not isinstance(variables, dict):
            continue
        return ThreadsGraphQLSession(
            lsd=extract_lsd(html),
            doc_id=str(preloader.get("queryID", "")),
            query_name=query_name,
            variables=dict(variables),
            user_id=str(variables.get("userID", "")),
            html=html,
        )

    raise ThreadsGraphQLError(
        code=None,
        summary="NoTimelineQuery",
        message=(
            "the profile page carried no timeline preloader, so the GraphQL query id could not be discovered"
        ),
        path=None,
    )


async def open_graphql_session(
    client: httpx.AsyncClient,
    username: str,
    *,
    cookie_header: str = "",
    user_agent: str | None = None,
) -> ThreadsGraphQLSession:
    """Fetch the profile page and mine it for everything GraphQL needs.

    Args:
        client: The HTTP client to use.
        username: The Threads handle.
        cookie_header: The ``Cookie`` header to send.
        user_agent: The user agent to present. Defaults to the first mobile
            agent.

    Returns:
        The session.

    Raises:
        ThreadsGraphQLError: When the page carries no timeline preloader.
    """
    html = await fetch_profile_html(
        client,
        username,
        cookie_header=cookie_header,
        user_agent=user_agent,
    )
    return open_graphql_session_from_html(html)


async def fetch_graphql_page(
    client: httpx.AsyncClient,
    session: ThreadsGraphQLSession,
    *,
    after: str | None = None,
    cookie_header: str = "",
    user_agent: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one page of a profile timeline.

    Args:
        client: The HTTP client to use.
        session: The session to page with.
        after: The cursor of the page to fetch. ``None`` fetches the first one.
        cookie_header: The ``Cookie`` header to send.
        user_agent: The user agent to present. Defaults to the first mobile
            agent.

    Returns:
        The post dicts of the page and its ``page_info``.

    Raises:
        ThreadsGraphQLError: When the endpoint returns an error, or answers
            with something that is not JSON -- which happens when the ``LSD``
            token has expired.
    """
    agent = user_agent or THREADS_USER_AGENTS[0]
    variables = dict(session.variables)
    if after:
        variables["after"] = after

    body = {
        "doc_id": session.doc_id,
        "variables": json.dumps(variables),
        "fb_api_req_friendly_name": session.query_name,
        "fb_api_caller_class": "RelayModern",
        "server_timestamps": "true",
        "lsd": session.lsd,
        "jazoest": jazoest(session.lsd),
    }
    headers = {
        "User-Agent": agent,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-FB-Friendly-Name": session.query_name,
        "X-FB-LSD": session.lsd,
        "Origin": "https://www.threads.net",
        "Referer": "https://www.threads.net/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    response = await client.post(GRAPHQL_ENDPOINT, headers=headers, data=body)

    # The content type is not a reliable signal here: Meta serves its JSON as
    # ``text/javascript`` and answers an invalid session with ``text/html``.
    # Parsing is the only reliable test.
    try:
        payload = response.json()
    except ValueError:
        raise ThreadsGraphQLError(
            code=None,
            summary="NotJSON",
            message=(
                f"the GraphQL endpoint answered with "
                f"{response.headers.get('content-type', 'an unknown type')} "
                f"instead of JSON ({len(response.text)} bytes); the session "
                "token has most likely expired"
            ),
            path=None,
        ) from None
    response.raise_for_status()

    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        if isinstance(first, dict):
            raise ThreadsGraphQLError(
                code=first.get("code"),
                summary=first.get("summary"),
                message=first.get("message"),
                path=first.get("path"),
            )
        raise ThreadsGraphQLError(summary="UnknownError", message=str(first))

    media = (payload.get("data") or {}).get("mediaData") or {}
    posts: list[dict[str, Any]] = []
    for edge in media.get("edges") or []:
        for item in (edge.get("node") or {}).get("thread_items") or []:
            post = item.get("post")
            if isinstance(post, dict) and "pk" in post:
                posts.append(post)
    return posts, media.get("page_info") or {}
