# osint-scraper

Scrape media from social media profiles. Phase one covers **Instagram** and **Threads**.

The tool discovers posts, downloads the media attached to them, and writes a JSONL
manifest recording exactly what was collected, from where and when.

## Status

| Platform | Engine | Auth required for public posts |
| --- | --- | --- |
| Instagram | `instaloader` 4.16 | No |
| Threads | GraphQL timeline over the web app's own endpoint | No |
| Facebook | planned | — |
| Search engines | planned | — |

Full status, risks and the decision log live in [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Install

```bash
git clone git@github.com:jcchikikomori/osint-scraper.git
cd osint-scraper
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,instagram]"
```

The `instagram` extra pulls in `instaloader`. It is optional at import time — the CLI tells you
what is missing rather than crashing.

## Usage

The handles below are placeholders — replace them with the account you want.

```bash
# Scrape an Instagram profile (public posts, no login needed)
osint-scraper scrape somehandle --platform instagram --output ./out

# Scrape a Threads profile
osint-scraper scrape somehandle --platform threads --output ./out

# Scrape one Threads post from a direct or share link
osint-scraper scrape "https://www.threads.com/share/D-VhmZm13/" --output ./out

# Both platforms, capped at 50 posts each, no downloads (discovery only)
osint-scraper scrape somehandle otherhandle --limit 50 --no-download

# Use the browser session: reaches private profiles, stories and highlights
osint-scraper scrape private_account --platform instagram --browser brave --profile "flatpak:Profile 1"

# Ignore the browser session entirely
osint-scraper scrape somehandle --platform instagram --no-cookies

# Inspect the environment: browsers, cookie databases, available tools
osint-scraper doctor

# List the browser profiles, with what each one holds and the spec to pass to --profile
osint-scraper cookies list --browser brave

# Export cookies from Brave into a Netscape cookies.txt
osint-scraper cookies export --browser brave --output ./brave-cookies.txt

# Inspect or clear the cooldown state
osint-scraper cooldown
osint-scraper cooldown --clear

# Re-run: media that was already downloaded is skipped
osint-scraper scrape somehandle --platform threads

# Inspect the download history
osint-scraper history
```

### Choosing a browser profile

By default **every profile of the browser is used and the cookies are merged**. The logged-in
session is usually in one profile while the remaining cookies live in the others, so merging
beats guessing. Where two profiles hold the same cookie, the one with the stronger session wins.

`osint-scraper cookies list` shows what each profile actually holds, and the exact spec to pass:

```console
$ osint-scraper cookies list --browser brave
[
  {
    "browser": "brave",
    "source": "system",
    "profile": "Default",
    "spec": "system:Default",
    "cookie_count": 9,
    "session_domains": ["facebook.com"]
  },
  {
    "browser": "brave",
    "source": "flatpak",
    "profile": "Profile 1",
    "spec": "flatpak:Profile 1",
    "cookie_count": 1181,
    "session_domains": ["instagram.com", "threads.net", "threads.com", "facebook.com"]
  }
]
```

To narrow it down, pass `--profile`. The name alone is enough when it is unique; when it is not,
the tool refuses to guess and tells you how to qualify it:

```console
$ osint-scraper scrape x --profile Default
error: profile 'Default' is ambiguous for brave (it exists in flatpak, system).
Qualify it: flatpak:Default, system:Default
```

| Option | Effect |
| --- | --- |
| `--profile "Profile 1"` | Use the profile with that directory name, if unique |
| `--profile "flatpak:Profile 1"` | Use that profile of that installation specifically |
| `--source flatpak` | Only look at Flatpak installations |
| `--source system` | Only look at system-wide installations |

The same options work on `osint-scraper cookies export`, and `cookies.profile` / `cookies.source`
in the config file set them permanently.

### Authenticated vs anonymous

Cookies are **optional**. Public Instagram posts and public Threads profiles need none. The browser
session is what reaches **private profiles, stories and highlights**.

With cookies enabled (the default), the Instagram crawler hands the browser's `instagram.com`
cookies to `instaloader` so it authenticates the same way the browser does. Without a
`sessionid` cookie it stays anonymous and says so in the log.

Verified on a private profile: authenticated yields the posts and their carousels; anonymous
raises `Login required to access the posts of private profile`.

### Cooldowns

When a platform pushes back, the tool starts a **cooldown** that is written to disk, so it also
applies to the *next* invocation. Retrying through a throttle is what turns a rate limit into a
permanent spam flag, so a run refuses to start while a host is cooling down:

```text
error: cooling down after being throttled: instagram.com for another 30 min.
Retrying now is what causes the flag -- wait it out.
Use `osint-scraper cooldown --clear` to override deliberately.
```

The cooldown lasts `ratelimit.cooldown_minutes` — 60 minutes by default. The pacing itself is
adaptive: the gap between requests grows on every throttle and relaxes again once the host stops
pushing back. The baseline is `delay` plus `jitter` from the config, and a fixed interval is
deliberately avoided — a constant cadence is a scraping signature.

### The cookie cache

Reading and decrypting a browser's cookie database takes a moment, and the tool does it on every
run. The result is cached in the store directory, keyed on **the selection** — the browser, the
profile and the source — so a cache written for one profile can never be served for another.

```bash
osint-scraper scrape somehandle --no-cookie-cache   # force a fresh read
```

The cache expires after `cookies.cache_ttl` seconds (15 minutes by default), and an expired
cookie is never served from it. The exported file and the cache are written `0600`, and the store
directory `0700`: these are live session cookies, and they were previously world-readable.

### Scrape a single post

Paste the link and get that one post — the same idea as `yt-dlp` with a direct video URL:

```bash
osint-scraper scrape "https://www.threads.com/share/D-VhmZm13/"
osint-scraper scrape "https://www.threads.net/@someone/post/AbCdEfG/"
```

A share link is resolved through its redirect, so the author and the post code are picked
up from the canonical URL. The post page also carries the surrounding conversation, so the
right post is selected by its code rather than taken as the first one.

You do not need `--platform`: a target that a platform does not recognise is skipped for
that platform and tried on the next one.

```console
$ osint-scraper scrape "https://www.threads.com/share/D-VhmZm13/"
{
  "targets": [ { "platform": "threads", "target": "https://www.threads.com/share/D-VhmZm13/",
                 "posts": 1, "downloaded": 1 } ],
  "skipped": [ { "platform": "instagram", "target": "...",
                 "reason": "Not an Instagram URL: https://www.threads.com/share/D-VhmZm13/" } ]
}
```

### Instagram surfaces

An Instagram account has more than one feed. The surface is selected by the **target URL**,
the way `yt-dlp` selects by URL — no extra flag to learn:

| Target | Surface |
| --- | --- |
| `instagram.com/<user>/` | the profile's posts |
| `instagram.com/<user>/tagged/` | the posts the account is tagged in |
| `instagram.com/stories/<user>/` | the account's active story |
| `instagram.com/<user>/highlights/` | every highlight, and every item in it |

```bash
osint-scraper scrape "https://www.instagram.com/stories/somehandle/"
osint-scraper scrape "https://www.instagram.com/somehandle/highlights/"
```

Stories and highlights need the browser session, and the manifest records which surface each
row came from.

### Full Threads history

The server-rendered profile page only carries the most recent 10–20 posts. The crawler
walks the **whole timeline** over the GraphQL endpoint the Threads web app itself uses:

```console
$ osint-scraper scrape somehandle --platform threads --no-download
INFO  threads: Threads page 38 for @somehandle: 5 new post(s), 215 so far
INFO  threads: Threads returned 215 post(s) for @somehandle
```

That is every post from 2023-08-03 to 2026-10-13 — against 10 through the old path.

The query id (`doc_id`) that the endpoint needs is **not hardcoded**. Meta rotates it
whenever it ships a new bundle, and a hardcoded value breaks without warning. The
server-rendered page embeds the preload manifest the server itself used, so the query id
and its exact variables are mined from the page at run time — a bundle rotation is picked
up on the next run instead of silently returning nothing.

If the transport breaks anyway, the run **falls back to the server-rendered page**: it
degrades to the recent posts rather than failing.

| Setting | Effect |
| --- | --- |
| `crawl.use_graphql` | `true` walks the whole timeline; `false` uses the page only |
| `crawl.max_pages` | How many GraphQL pages to walk per target |

The GraphQL query is deliberately **anonymous**. A logged-in request is answered with a
different, non-paginating variant of the query, and the session cookie makes the endpoint
reply with HTML instead of JSON. The browser session is still used for the
server-rendered page and for Instagram.

### Re-running and resuming

A second run does **not** download the same media again. Every download is recorded in a
SQLite history database (`~/.cache/osint-scraper/history.db`) together with the file's SHA-256,
and the next run checks the file that is already on disk before it fetches anything:

```console
$ osint-scraper scrape somehandle --platform threads
...
INFO  threads: 2026-10-28_13-38-41_DfCjvSCEbct.mp4 already downloaded, skipping
{
  "downloads": { "downloaded": 0, "skipped": 2, "duplicates": 0, "resumed": 0, "failed": 0 }
}
```

Three things make that work:

| Mechanism | What it does |
| --- | --- |
| **Stable media key** | Instagram and Threads serve media from a CDN whose URL carries a short-lived signature — the same image arrives under a *different* URL every run. The history is therefore keyed on the URL path without its query string, so the media is still recognised. |
| **Hash check** | Before an existing file is trusted, its SHA-256 is compared with the recorded digest. A mismatch means the platform changed the media, so it is fetched again. A file you deleted is fetched again too. |
| **Partial resume** | An interrupted download leaves a `.part` file behind. The next run asks the server for the remainder with a `Range` header instead of starting over. |

Media whose content is already in the library under a different name is not stored twice — the
record points at the copy that already exists.

```console
# Turn the resume off and fetch everything again
osint-scraper scrape somehandle --platform threads --no-resume

# Skip the re-hash and compare file sizes only (much cheaper on a large library)
osint-scraper scrape somehandle --platform threads --no-verify

# Inspect the history, or drop the entries whose file you deleted
osint-scraper history
osint-scraper history --prune
```

The history lives outside the output directory on purpose: deleting the media does not delete the
knowledge that it was there, and `--prune` is how you tell the tool that.

### Output layout

```text
out/
├── instagram/
│   └── somehandle/
│       ├── 2026-10-17_20-00-01_DeZtp-TkQHh.jpg
│       ├── 2026-10-17_20-00-01_DeZtp-TkQHh.json
│       └── ...
├── threads/
│   └── somehandle/
│       └── ...
└── manifest.jsonl
```

`manifest.jsonl` holds one JSON object per crawled post, one per **media object** attached to a
post, and one per downloaded file. It is the audit trail of a run:

```json
{"kind":"file","platform":"threads","target":"somehandle","post_id":"4006434140014576097","url":"https://...","path":"threads/somehandle/2026-10-17_20-00-01_DeZtp-TkQHh.jpg","sha256":"…","size":284913}
```

A carousel used to be indistinguishable from three unrelated posts — every child shared the
parent's `post_id`, and its only identity was a `_1` / `_2` / `_3` filename suffix. Each media
object now gets its own `"media"` row, carrying its position, its kind, its dimensions and the
platform's own id for it:

```json
{"kind":"media","platform":"instagram","target":"somehandle","post_id":"4011374380604567319","url":"https://...","metadata":{"index":0,"media_id":"4016734873234909654_787132","kind":"image","width":1080,"height":1350,"mime":"image/jpeg","extension":".jpg","shortcode":"DerQ775"}}
```

The `"media"` row records what was *discovered*; the `"file"` row records what was *stored*.

## Configuration

Settings are layered: built-in defaults, then a YAML file, then CLI flags. The file is looked up
in `$OSINT_SCRAPER_CONFIG`, `./osint-scraper.yaml`, `$XDG_CONFIG_HOME/osint-scraper/config.yaml`
and `~/.config/osint-scraper/config.yaml`, in that order.

See [`config/default.yaml`](config/default.yaml) for every option with its default.

```yaml
crawl:
  limit: 0                 # 0 = unlimited
  max_posts_per_target: 5000  # safety ceiling, so a runaway target cannot fill the disk
  include_text_only: true  # keep posts that carry no media (a caption and nothing else)
  platforms: [instagram, threads]
download:
  directory: ./osint-output
  concurrency: 4
  delay: 1.8               # seconds between requests to the same host
  jitter: 2.7              # extra random delay on top of `delay`
  save_metadata: true
cookies:
  browser: brave
  profile: "Profile 1"     # empty = merge every profile of the browser
  flatpak: true
history:
  database: ~/.cache/osint-scraper/history.db
  resume: true             # skip media that was already downloaded
  verify: true             # re-hash existing files and compare the digest
  dedupe: true             # do not store the same content twice
  prune: true              # forget entries whose file was deleted
```

## Browser cookies

Cookies are optional. Public Instagram posts and public Threads profiles need none. They are
required for stories, highlights, follow-gated content and the full Threads history.

Cookies are read from your own browser on your own machine, in two places:

- **System installs** — `~/.config/<browser>/<Profile>/Cookies`
- **Flatpak installs** — `~/.var/app/<app-id>/config/.../Cookies`

Brave is supported first-class, alongside Chrome, Chromium, Edge and Firefox. Chromium-family
cookie databases are encrypted; this tool decrypts the `v10`/`v11` schemes in-process and reads
the OS keyring through `secret-tool` when a key is needed. Firefox stores cookies in plaintext.

Full details, including what to do when a Flatpak browser reports the unreadable `v12` scheme,
are in [`docs/COOKIES.md`](docs/COOKIES.md).

## Legal and ethical use

This tool is for collecting **publicly accessible** media for research, archiving, journalism or
incident response. It does not access private, protected or follow-gated accounts, does not
circumvent CAPTCHAs or access controls, and does not use multiple accounts to defeat rate limits.

Meta's Terms of Use prohibit automated data collection, and `threads.com/robots.txt` disallows
all crawlers except a named allowlist. `hiQ v. LinkedIn` (9th Cir.) protects the scraping of
publicly accessible data against a breach-of-contract claim, but not against CFAA claims or
against the platform's own IP rights. **This is not legal advice** — get a lawyer for anything
commercial, and record the legal basis for each collection you run.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest
```

Quality gates also run through pre-commit:

```bash
pipx install pre-commit
pre-commit install
pre-commit run --all-files
```

## License

MIT — see [`LICENSE`](LICENSE).
