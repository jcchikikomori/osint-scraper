# osint-scraper

Scrape media from social media profiles. Phase one covers **Instagram** and **Threads**.

The tool discovers posts, downloads the media attached to them, and writes a JSONL manifest
recording exactly what was collected, from where and when. A second run downloads nothing it
already has.

## Status

| Platform | Engine | Auth needed for public posts |
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

The `instagram` extra pulls in `instaloader`; the `cookies` extra pulls in `secretstorage`, which
is needed only for Chromium `v12` cookies. Both are optional — the CLI says what is missing
rather than crashing.

## Quick start

```bash
# Scrape a profile's public posts
osint-scraper scrape somehandle --platform instagram --output ./out

# Scrape a Threads profile — the whole timeline, not just the recent posts
osint-scraper scrape somehandle --platform threads --output ./out

# Scrape one post from a direct or share link
osint-scraper scrape "https://www.threads.com/share/HydvsaQAW/" --output ./out

# Use the browser session: reaches private profiles, stories and highlights
osint-scraper scrape somehandle --platform instagram --browser brave --profile "flatpak:Profile 1"

# Ignore the browser session entirely
osint-scraper scrape somehandle --platform instagram --no-cookies

# Discovery only — list what is there without fetching the media
osint-scraper scrape somehandle --platform threads --no-download
```

The handles above are placeholders — replace them with the account you want. `--platform` is
optional: a target a platform does not recognise is skipped for that platform and tried on the
next one, so a Threads link does not need it.

## What you can target

The surface is selected by the **target URL**, the way `yt-dlp` selects by URL.

| Target | Gets |
| --- | --- |
| `somehandle` | The profile's posts |
| `instagram.com/<user>/` | The profile's posts |
| `instagram.com/<user>/tagged/` | The posts the account is tagged in |
| `instagram.com/stories/<user>/` | The account's active story |
| `instagram.com/<user>/highlights/` | Every highlight, and every item in it |
| `threads.net/@<user>/` | The whole timeline |
| `threads.net/@<user>/post/<code>/` | One post |
| `threads.com/share/<code>/` | One post, through its redirect |

## The options that matter

| Option | Effect |
| --- | --- |
| `--platform` | Restrict the run to one platform |
| `--limit` | Maximum posts per target |
| `--output`, `-o` | Output directory. Defaults to `./osint-output` |
| `--no-download` | Discover media without fetching it |
| `--no-cookies` | Stay anonymous; read nothing from your browser |
| `--browser`, `--profile`, `--source` | Which browser session to use |
| `--no-cookie-cache` | Re-read the browser instead of using a cached extraction |
| `--no-resume` | Fetch everything again, ignoring what is already downloaded |
| `--history` | Path to the history database |

## Output

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

The manifest holds one row per post, one per media object and one per downloaded file, with the
source URL, the timestamp and the SHA-256 digest of the bytes. It is the audit trail of the
run.

## Configuration

Settings are layered: built-in defaults, then a YAML file, then CLI flags. The file is looked
up in `$OSINT_SCRAPER_CONFIG`, `./osint-scraper.yaml`, `$XDG_CONFIG_HOME/osint-scraper/config.yaml`
and `~/.config/osint-scraper/config.yaml`, in that order.

[`config/default.yaml`](config/default.yaml) lists every option with its default and a comment
explaining what it does.

## Further reading

| Document | What is in it |
| --- | --- |
| [`docs/USAGE.md`](docs/USAGE.md) | Every command, every option, every target form |
| [`docs/RESUME.md`](docs/RESUME.md) | How the history, the resume, the dedupe and the manifest work |
| [`docs/COOKIES.md`](docs/COOKIES.md) | Which browser profile is used, and how the encryption schemes are handled |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The layer and module map, and why it is shaped that way |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Milestones, the risk register and the decision log |
| [`docs/TARGETS.md`](docs/TARGETS.md) | What the cookie subsystem can reach |
| [`CLAUDE.md`](CLAUDE.md) | The maintainer's notes and the facts verified against the live platforms |

## Legal

**Read [`DISCLAIMER.md`](DISCLAIMER.md) before you collect anything.** It covers what the tool
does and does not do, the terms of service of the platforms involved, copyright, data
protection, and the responsibilities that come with running it.

The short version: collect **publicly accessible** media, for research, archiving, journalism or
incident response. It does not access private or follow-gated accounts, does not circumvent
CAPTCHAs or access controls, and does not use multiple accounts to defeat rate limits. **This
is not legal advice** — record the legal basis for each collection you run, and get a lawyer
for anything commercial.

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
