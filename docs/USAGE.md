# Usage

The complete command and option guide. The [README](../README.md) has the quick start; this is
the long read.

## Contents

- [Commands](#commands)
- [Targets](#targets)
- [Instagram surfaces](#instagram-surfaces)
- [Single posts and share links](#single-posts-and-share-links)
- [Authenticated or anonymous](#authenticated-or-anonymous)
- [Cooldowns and rate limiting](#cooldowns-and-rate-limiting)
- [Options](#options)
- [Configuration](#configuration)
- [Re-running and resuming](#re-running-and-resuming)

## Commands

| Command | What it does |
| --- | --- |
| `scrape` | Crawl the given targets and download their media |
| `cookies list` | List the browser profiles that can be used as a cookie source |
| `cookies export` | Export browser cookies into a Netscape `cookies.txt` |
| `cooldown` | Show, or clear, the stored cooldown state |
| `history` | Show the download history, or prune the entries whose file is gone |
| `doctor` | Report on the local environment: browsers, cookie databases, tools |
| `platforms` | List the registered crawlers |

Every command takes `--config`, `--verbose` and `--quiet` as global options. The `--config`
option goes **before** the subcommand:

```bash
osint-scraper --config ./osint-scraper.yaml scrape somehandle
```

## Targets

A target is anything the tool can turn into posts: a handle, a profile URL, a post URL, a share
link, or — for Instagram — one of a profile's surfaces.

```bash
osint-scraper scrape somehandle --platform instagram
osint-scraper scrape somehandle --platform threads
osint-scraper scrape somehandle otherhandle --limit 50
```

`--platform` restricts the run to one crawler. Without it, the tool tries each configured
platform in turn, and a target that a platform does not recognise is **skipped** for that
platform rather than being an error — so a Threads link is not killed by the Instagram crawler.

## Instagram surfaces

An Instagram account has more than one feed. The surface is selected by the **target URL**,
the way `yt-dlp` selects by URL, so there is no extra flag to learn:

| Target | Surface | Needs a session |
| --- | --- | --- |
| `instagram.com/<user>/` | the profile's posts | No |
| `instagram.com/<user>/tagged/` | the posts the account is tagged in | Yes |
| `instagram.com/stories/<user>/` | the account's active story | Yes |
| `instagram.com/<user>/highlights/` | every highlight, and every item in it | Yes |

```bash
osint-scraper scrape "https://www.instagram.com/stories/somehandle/"
osint-scraper scrape "https://www.instagram.com/somehandle/highlights/"
osint-scraper scrape "https://www.instagram.com/somehandle/tagged/"
```

Each manifest row records the surface it came from in its `metadata.surface` field.

**IGTV is broken upstream.** `get_igtv_posts()` raises `KeyError: 'edges'` because the GraphQL
response shape changed. The tool reports it as an error naming the cause rather than letting a
raw `KeyError` escape. IGTV videos also appear in the regular profile feed, so scraping the
profile URL still gets them.

## Single posts and share links

Paste the link and get that one post — the same idea as `yt-dlp` with a direct video URL:

```bash
osint-scraper scrape "https://www.threads.com/share/HydvsaQAW/"
osint-scraper scrape "https://www.threads.net/@someone/post/AbCdEfG/"
```

A share link is resolved through its redirect, so the author and the post code are picked up
from the canonical URL. The post page also carries the surrounding conversation, so the right
post is selected by its code rather than taken as the first one.

You do not need `--platform` — a target that a platform does not recognise is skipped for that
platform and tried on the next one:

```console
$ osint-scraper scrape "https://www.threads.com/share/HydvsaQAW/"
{
  "targets": [ { "platform": "threads", "target": "https://www.threads.com/share/HydvsaQAW/",
                 "posts": 1, "downloaded": 1 } ],
  "skipped": [ { "platform": "instagram", "target": "...",
                 "reason": "Not an Instagram URL: https://www.threads.com/share/HydvsaQAW/" } ]
}
```

## Authenticated or anonymous

Cookies are **optional**. Public Instagram posts and public Threads profiles need none. The
browser session is what reaches **private profiles, stories, highlights and follow-gated
content**.

| | Anonymous (`--no-cookies`) | Authenticated (default) |
| --- | --- | --- |
| Public posts | Yes | Yes |
| Private or follow-gated content | No — the platform refuses | Yes, with your session |
| Stories and highlights | No | Yes |
| Threads history | The most recent 10–20 posts | The whole timeline |
| Reads your browser | No | Yes — the cookie database and the keyring |

With cookies enabled, the Instagram crawler hands the browser's `instagram.com` cookies to
`instaloader` so it authenticates the same way the browser does. Without a `sessionid` cookie
it stays anonymous and says so in the log.

Verified on a private profile: authenticated yields the posts and their carousels; anonymous
raises `Login required to access the posts of private profile`.

See [`COOKIES.md`](COOKIES.md) for which browser profile is used, where the cookies are read
from, and how the encryption schemes are handled.

## Cooldowns and rate limiting

When a platform pushes back, the tool starts a **cooldown** that is written to disk, so it also
applies to the *next* invocation. Retrying through a throttle is what turns a rate limit into a
permanent spam flag, so a run refuses to start while a host is cooling down:

```text
error: cooling down after being throttled: instagram.com for another 30 min.
Retrying now is what causes the flag -- wait it out.
Use `osint-scraper cooldown --clear` to override deliberately.
```

The cooldown lasts `ratelimit.cooldown_minutes` — 60 minutes by default. The pacing itself is
adaptive: the gap between requests grows on every throttle and relaxes again once the host
stops pushing back. The baseline is `delay` plus `jitter` from the config, and a fixed interval
is deliberately avoided — a constant cadence is a scraping signature.

```bash
osint-scraper cooldown            # what is cooling down, and for how long
osint-scraper cooldown --clear     # override deliberately
```

Setting `ratelimit.enabled: false` turns cooldown enforcement off entirely. It is there for
testing the throttle itself; leaving it on is the safer default.

## Options

### `scrape`

| Option | Effect |
| --- | --- |
| `--platform`, `-p` | Restrict the run to one platform. Repeatable. Omit to try each in turn |
| `--limit`, `-n` | Maximum posts per target. `0` means unlimited |
| `--output`, `-o` | Output directory. Defaults to `./osint-output` |
| `--download` / `--no-download` | Download the discovered media. On by default |
| `--dry-run` | Discover media but download nothing |
| `--manifest` / `--no-manifest` | Write `manifest.jsonl` next to the media. On by default |
| `--cookies` / `--no-cookies` | Use the browser session. On by default |
| `--browser`, `-b` | Which browser to take cookies from |
| `--profile` | Which profile to use, e.g. `'Profile 1'` or `'flatpak:Profile 1'` |
| `--source` | Restrict to one installation: `system`, `flatpak` or `any` |
| `--cookie-cache` / `--no-cookie-cache` | Reuse a recent cookie extraction. On by default |
| `--resume` / `--no-resume` | Skip media that was already downloaded. On by default |
| `--verify` / `--no-verify` | Re-hash existing files and compare the digest. On by default |
| `--dedupe` / `--no-dedupe` | Do not store the same content twice. On by default |
| `--history` | Path to the history database |

### `cookies list`

| Option | Effect |
| --- | --- |
| `--browser`, `-b` | Restrict the listing to one browser |
| `--flatpak` / `--no-flatpak` | Include Flatpak sandboxed profiles. On by default |
| `--probe` / `--no-probe` | Decrypt each profile to count its cookies and detect sessions. On by default |

### `cookies export`

| Option | Effect |
| --- | --- |
| `--browser`, `-b` | Browser to export from. Defaults to `brave` |
| `--profile` | Which profile to export. Omit to merge every profile |
| `--source` | Restrict to one installation |
| `--output`, `-o` | Destination `cookies.txt` path |
| `--check` / `--no-check` | Report whether an authenticated session was found. On by default |

### `cooldown`

| Option | Effect |
| --- | --- |
| `--clear` | Forget the stored cooldown state |
| `--host` | Show, or clear, the state of one host only |

### `history`

| Option | Effect |
| --- | --- |
| `--prune` | Drop the rows whose file is gone |
| `--history` | Path to the history database |

## Configuration

Settings are layered: built-in defaults, then a YAML file, then CLI flags. The file is looked up
in `$OSINT_SCRAPER_CONFIG`, `./osint-scraper.yaml`, `$XDG_CONFIG_HOME/osint-scraper/config.yaml`
and `~/.config/osint-scraper/config.yaml`, in that order.

See [`config/default.yaml`](../config/default.yaml) for every option with its default and a
comment explaining what it does.

```yaml
crawl:
  limit: 0                    # 0 = unlimited
  max_posts_per_target: 5000  # safety ceiling, so a runaway target cannot fill the disk
  include_text_only: true     # keep posts that carry no media (a caption and nothing else)
  platforms: [instagram, threads]
  use_graphql: true           # walk the whole Threads timeline rather than the recent posts
  max_pages: 100              # GraphQL pages to walk per target
download:
  directory: ./osint-output
  concurrency: 4
  delay: 1.8                  # seconds between requests to the same host
  jitter: 2.7                 # extra random delay on top of `delay`
  save_metadata: true
cookies:
  browser: brave
  profile: "Profile 1"        # empty = merge every profile of the browser
  flatpak: true
  use_cache: true             # reuse a recent extraction instead of reading the browser
  cache_ttl: 900              # seconds a cached extraction stays usable
ratelimit:
  enabled: true
  cooldown_minutes: 60
history:
  database: ~/.cache/osint-scraper/history.db
  resume: true                # skip media that was already downloaded
  verify: true                # re-hash existing files and compare the digest
  dedupe: true                # do not store the same content twice
  prune: true                 # forget entries whose file was deleted
```

## Re-running and resuming

A second run does **not** download the same media again. The mechanics — the stable media key,
the hash check, the partial resume, the manifest format — are in
[`RESUME.md`](RESUME.md).
