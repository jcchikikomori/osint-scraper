# Architecture

The project follows the shape of [`cyberdrop-dl-patched`](https://github.com/Cyberdrop-DL/cyberdrop-dl)
— site-handler modules, YAML configuration, a central cookie layer, SQLite history and a JSONL
manifest — scaled down from 245 file hosts to a handful of social platforms.

## Layers

```text
                    ┌───────────────┐
                    │     cli.py    │  argument parsing, orchestration
                    └───────┬───────┘
                            │
              ┌─────────────┴─────────────┐
              │                           │
      ┌───────▼────────┐          ┌───────▼────────┐
      │  crawlers/     │          │   downloader   │
      │  discovery     │          │   transport    │
      └───────┬────────┘          └───────┬────────┘
              │                           │
              └─────────────┬─────────────┘
                            │
                  ┌─────────▼──────────┐
                  │  cookies.py        │  one cookie source for every platform
                  │  manifest.py       │  the audit trail
                  │  database.py       │  resume / history
                  └────────────────────┘
```

The key boundary is between **discovery** and **transport**. A crawler turns a handle into a
stream of `Post` objects and never touches the filesystem. The downloader fetches media and never
performs discovery. Both sides are independently testable, and a third platform can be added
without touching either.

## Modules

| Module | Responsibility |
| --- | --- |
| `models.py` | `Post`, `MediaRef`, `ProfileInfo`, `CrawlStats` — the shared contract |
| `crawlers/base.py` | `Crawler` ABC, `CrawlContext`, the crawler registry |
| `crawlers/instagram.py` | Instagram discovery via `instaloader` |
| `crawlers/threads.py` | Threads discovery: GraphQL timeline, single posts, SSR fallback |
| `crawlers/threads_api.py` | The Threads transport: user agents, the GraphQL session, its pages |
| `cookies.py` | Browser profile discovery, Chromium/Firefox decryption, `cookies.txt` export |
| `downloader.py` | Concurrent, rate-limited, hashed media download; resume, file check and dedupe |
| `database.py` | SQLite history: media index (resume), content index (dedupe), seen posts |
| `manifest.py` | Append-only JSONL manifest |
| `filepath.py` | Filename sanitisation, extension guessing, collision-free paths |
| `config/settings.py` | Layered settings (defaults → YAML → CLI) |
| `cli.py` | `scrape`, `cookies`, `history`, `doctor`, `platforms` |

## Adding a platform

1. Create `crawlers/<name>.py` with a `Crawler` subclass.
2. Set `PLATFORM`, `DISPLAY_NAME`, `REQUIRES_AUTH`.
3. Implement `async def crawl(self, target, *, limit=None)` as an async generator yielding `Post`.
4. Register it with `@register_crawler`.
5. Add tests with a fixture — never hit the network in a test.

That is the whole contract. The CLI, the downloader, the manifest and the cookie layer pick the
new platform up automatically.

## Design decisions

### Discovery by structural signature, not by JSON path
Meta's web payloads are nested inside a framework envelope whose shape changes between releases.
The Threads crawler therefore walks the parsed object graph and collects anything matching a
signature — *has `pk` and `code`, and at least one of `caption`, `image_versions2`,
`video_versions`, `text_post_app_info`* — instead of following a fixed path.

The consequence is deliberate: a refactor of the envelope should degrade the results, not zero
them. When it does break, the crawler raises a `ParseError` carrying the response size, the
number of JSON script blocks found and the user agents tried, so the failure is diagnosable
without a debugger.

### The Threads query id is discovered, never hardcoded

The Threads web app reads a profile's timeline over a Relay GraphQL endpoint. The endpoint
needs the numeric id of the persisted query — the `doc_id` — and Meta rotates it whenever a
new bundle ships. A hardcoded id therefore breaks without warning.

The server-rendered profile page solves this: it embeds the preload manifest the server
itself used, which names the query, its `doc_id` and its exact `variables`. The session is
mined from that page at run time, so a bundle rotation is picked up on the next run. The
`variables` are copied **verbatim** — they carry around fifty feature flags that change with
the bundle, and hardcoding them is how the transport breaks.

The query is deliberately **anonymous**. A logged-in request is answered with a different,
non-paginating variant of the query, and the session cookie makes the endpoint reply with
HTML instead of JSON. The browser session still serves the server-rendered page and
Instagram.

The server-rendered page remains the fallback, so a transport that breaks degrades the
result to the recent posts rather than failing the run.

### Two independent engines for Instagram

Meta breaks private endpoints every few weeks, and the two available engines are patched on
different schedules. `instaloader` is the primary; `gallery-dl` is an independent second
extractor so that when one breaks the other still covers the target. Both are pinned.

### One cookie source for every platform

Cookie handling is centralised in `cookies.py` and emits a Netscape `cookies.txt`. That format is
understood by `instaloader`, `gallery-dl` and `yt-dlp` alike, so there is exactly one place to
update when Chromium introduces a new cipher tier — and one artefact to manage.

### Politeness is structural, not optional

The downloader spaces requests to the same host by `random.uniform(delay, delay + jitter)`. A
fixed cadence is a scraping signature; a jittered one is not. The delay is on by default and the
unauthenticated posture is preferred, because an account that is never used cannot be
restricted.

### Manifest as the audit trail

Every run appends to `manifest.jsonl`: one line per downloaded file, one per crawled post, each
carrying the source URL, the SHA-256 of the bytes on disk, the size and the post metadata. The
manifest is flushed after every record, so an interrupted run still leaves a complete-so-far
record of what was collected, from where, and when.

### The history is keyed on the media, not the URL

Instagram and Threads serve media from a CDN whose URL carries a short-lived signature. The same
image is served under a *different* full URL on every run, so a history keyed on the URL would
never match and a resume would download everything again. The history is therefore keyed on the
URL **path without its query string**, and two indexes are kept:

- `media` — where each piece of media is, and what its digest is. This drives the resume.
- `hashes` — what content the library holds. This drives the dedupe.

Before an existing file is trusted it is re-hashed and compared with the recorded digest: a
mismatch means the platform changed the media, and the file is fetched again. A deleted file is
fetched again too, which is what `history --prune` takes care of when the file is removed from
underneath the tool.

This mirrors `cyberdrop-dl`, which keeps the same split between a resume table and a hash table.
Its source is available read-only under `.slim/clonedeps/repos/Cyberdrop-DL__cyberdrop-dl/`.
