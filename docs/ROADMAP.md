# osint-scraper roadmap and tracker

This is the running tracker for the project. It records what is done, what is in
flight, what is next, and — just as importantly — **why** each decision was made.
Update it whenever a milestone moves.

Last updated: 2026-10-30

## Status legend

| Marker | Meaning |
| --- | --- |
| `[ ]` | Not started |
| `[~]` | In progress |
| `[x]` | Done and verified |
| `[!]` | Blocked — see the note |
| `[-]` | Deliberately not doing |

## Milestones

### M0 — Foundation

- [x] Project skeleton, `pyproject.toml` (hatchling, `src/` layout, `ruff` + `mypy` strict + `pytest`)
- [x] Core data contract: `Post`, `MediaRef`, `ProfileInfo`, `CrawlStats`
- [x] Crawler base class + registry (`crawlers/base.py`)
- [x] Layered settings (`config/settings.py`) with YAML override
- [x] Filename sanitisation and extension guessing (`filepath.py`)
- [x] JSONL manifest writer (`manifest.py`)
- [x] CLI skeleton (`cli.py`): `scrape`, `cookies`, `doctor`, `platforms`
- [x] Custom exception hierarchy (`exceptions.py`)

### M1 — Instagram (phase one)

- [x] Engine: **instaloader 4.16** — verified live, downloads public profile posts with no login
- [x] `InstagramCrawler` with `crawl()` / `profile()`
- [x] Sidecar/carousel media enumeration via `get_displayable_urls()`
- [x] Media kind + MIME mapping, metadata extraction (likes, comments, media type)
- [x] `limit` support
- [x] Login-wall detection mapped to `AuthError` with actionable remediation
- [x] **Authenticated mode** — the browser's `instagram.com` cookies are handed to instaloader via
      `Instaloader.load_session()`, so private profiles, stories and highlights are reachable
- [x] Profile selection prefers a profile holding a live session over one with more anonymous cookies
- [x] Unit tests with a fake instaloader (no network)
- [x] **Carousel children as first-class manifest rows** — each media object attached to a post
      now gets its own `"media"` row in the manifest, carrying its position, its kind, its
      dimensions and the platform's own id for it. A carousel used to be indistinguishable from
      three unrelated posts: every child shared the parent's `post_id` and its only identity was
      a `_1` / `_2` / `_3` filename suffix.
- [x] **Stories / highlights / tagged posts** — selected by the target URL, the way `yt-dlp`
      selects by URL: `instagram.com/<user>/tagged/`, `instagram.com/stories/<user>/`,
      `instagram.com/<user>/highlights/`. All three verified live: the active story reel, **135
      highlight items across 5 highlights**, and the posts a target is tagged in. They are reached
      through the iPhone API, which needs the authenticated session.
- [!] **IGTV is broken upstream.** `profile.get_igtv_posts()` raises `KeyError: 'edges'` — the
      GraphQL response shape changed. The crawler reports it as a domain error naming the cause
      rather than letting a raw `KeyError` escape. IGTV videos also appear in the profile feed.
- [ ] Secondary engine: gallery-dl (currently broken — see risk R3). **Verified 2026-10-30:**
      gallery-dl 1.32.18's Instagram extractor mines its `doc_id` at run time from the JS bundles,
      but the page it fetches carries **no bundle references at all** — it requests the profile
      with `Sec-Fetch-Site: none` / `Sec-Fetch-Mode: navigate`, and Instagram answers that with a
      778 KB page containing zero `.js` URLs. The same URL fetched with a plain user agent
      returns 512 KB carrying six bundles, and the query id mines out of them. The technique is
      sound; gallery-dl's page fetch is what is broken.

### M2 — Threads (phase one)

- [x] Discovery method: Threads **mobile-app User-Agent** against the public profile page
- [x] `__NEXT_DATA__`-free envelope handling — the payload lives in SvelteKit/Comet `data-sjs` scripts
- [x] Signature-based recursive extraction (no hardcoded JSON paths)
- [x] Image / video / carousel media selection
- [x] Quoted-post handling (quoted post is attached to its parent, never emitted top-level)
- [x] Engagement metadata (`like_count`, `direct_reply_count`) — the official API does **not** expose these
- [x] `profile()` from the same payload
- [x] `ParseError` with diagnostics when the envelope changes
- [x] Unit tests with an inline HTML fixture (no network)
- [x] Full history is reachable over the **GraphQL transport**. Meta's server-rendered ceiling is
      roughly the 10–20 most recent posts per profile, but the GraphQL transport walks the whole
      timeline — **215 posts for `@somehandle` (2023-08-03 → 2026-10-13)** against 10 through the
      page. The server-rendered page is kept as the fallback.
- [!] **A private Threads profile yields no posts at all.** Threads does not server-render private
      profiles, so the page carries the user object (`text_post_app_is_private: true`) but no posts.
      The crawler detects this and says so instead of blaming the user agent. The GraphQL transport
      does **not** help: it answers `1675030` for a private profile (risk R12), so private Threads
      profiles stay unreachable.
- [x] Threads authentication: the session cookie lives on `.threads.com` while the site is served
      from `threads.net`, so both domains are merged into one `Cookie` header
- [x] Empty-payload retries that rotate the user agent, for the intermittently served shell
- [x] **Full timeline over the GraphQL transport** — `POST /api/graphql`, with the query id
      (`doc_id`) and its exact variables **discovered from the server-rendered page at run
      time** rather than hardcoded, so a bundle rotation is picked up automatically.
      **Verified live: 215 posts for `@somehandle` (2023-08-03 → 2026-10-13) against 10 through
      the server-rendered page.**
- [x] The GraphQL query is **anonymous**: a logged-in request returns a different,
      non-paginating variant of the query, and the session cookie makes the endpoint answer
      with HTML instead of JSON
- [x] Server-rendered page kept as the **fallback**, so a broken transport degrades the
      result instead of failing the run
- [x] **Direct post links** — a post URL, a `/share/` link or a `/t/` short link is
      resolved to the one post it names, the way `yt-dlp` takes a direct video URL
- [x] A target a platform does not recognise is skipped for that platform and tried on the
      next one, so a Threads link no longer needs `--platform`
- [ ] Reply-tree traversal (currently only top-level posts of the profile)
- [ ] Private Threads profiles: the GraphQL transport answers `1675030` for them, so they
      stay unreachable. Tracked as risk R12.

### M3 — Browser cookies (phase two, but the foundation is landed)

- [x] Profile discovery: Brave, Chrome, Chromium, Edge (system **and** Flatpak), Firefox
- [x] Chromium `v10` / `v11` decryption (PBKDF2-HMAC-SHA1 → AES-128-CBC, 32-byte integrity prefix stripped)
- [x] Keyring password retrieval via `secret-tool` — **all** candidate entries are tried and the
      one that actually decrypts wins, because a desktop can hold several entries for the same key
- [x] Firefox plaintext `cookies.sqlite` reader (incl. Firefox 147+ XDG profile roots)
- [x] Netscape `cookies.txt` export — consumable by instaloader, gallery-dl and yt-dlp alike
- [x] Session detection (`sessionid` / `c_user` / `xs`) and an explicit "not logged in" warning
- [x] `v12` (Flatpak SecretPortal) detection with remediation guidance
- [x] Verified live against the operator's Flatpak Brave **Profile 1**: 1177 cookies decrypted,
      including `sessionid` for both Instagram and Threads
- [x] **Verified end-to-end on a private profile**: `private_account` (private) yields 5 posts and 16
      media files authenticated, and `LoginRequiredException` anonymously — the session is what
      makes the difference
- [x] Profile selection prefers a profile holding a live session over one with more anonymous cookies
- [x] **Every profile is merged by default**; `--profile` / `--source` narrow it, and an ambiguous
      bare name is refused rather than guessed
- [x] `osint-scraper cookies list` reports each profile's cookie count and session domains
- [x] Persistent cooldown state: a throttle stops the next run, not just the current one
- [x] **The `v12` case is resolved, not merely reported.** The old advice was wrong on two
      counts: `v12` is not Flatpak-exclusive in principle, and the `chrome://flags`
      `#password-store-basic` option does not exist. `v12` is Chromium's `os_crypt_async`
      SecretPortal scheme — HKDF-SHA256 into **AES-256-GCM** — and the portal secret is
      persisted in the host keyring, where a Secret Service client can read it. The detection
      path stays as the fallback when the secret cannot be found.
- [x] **Exported cookie files and the extraction cache carry restrictive permissions** — `0600`
      for the files, `0700` for the store directory, applied at creation rather than afterwards
      so the file is never world-readable even mid-write. The temporary copy of the browser's
      cookie database was leaking `0644` too, and no longer does.
- [ ] Rotate the exported cookie file — an export is overwritten in place today, but nothing
      forces a fresh one to replace an old export on disk.
- [x] **Cookie file reuse from the store dir** — the extraction is cached per selection, so a
      second run does not re-read and re-decrypt the browser's cookie database. The cache is
      keyed on the browser, the profile and the source, expires after `cookies.cache_ttl`, and
      never serves an expired cookie.

### M4 — Search engines (phase three)

- [ ] `ddgs` (successor of `duckduckgo-search`) as the metasearch backend
- [ ] Target discovery: handle → candidate profiles across search engines
- [ ] Handle-to-profile verification (avoid false positives)
- [ ] Rate-limit and backoff per backend
- [ ] Structured "discovery" output so the user can review candidates before scraping

### M5 — Facebook (phase three)

- [ ] Evaluate `facebook-scraper` (PyPI release stale since 2022; 447 open issues — treat as unmaintained)
- [ ] Evaluate `yt-dlp`'s Facebook extractors as the primary transport
- [ ] Album / timeline / photos_and_videos sub-scrapers
- [ ] Reuse the cookie adapter (Facebook cookies come from the same Brave profile)

### M6 — Reporting and hardening

- [x] **Adaptive pacing and persistent cooldowns** — jittered delays, a multiplier that grows when
      pushed back and relaxes afterwards, and a cooldown that survives the process
- [x] `osint-scraper cooldown` to inspect and clear the cooldown state
- [x] A run refuses to start while a platform is cooling down
- [x] SQLite history wired into the downloader so re-runs skip known media
- [x] **Resume, file check and hash dedupe** — a re-run downloads nothing it already has:
      the history is keyed on the media (the URL path without its CDN signature, which rotates on
      every run), an existing file is re-hashed and compared with the recorded digest before it is
      trusted, and an interrupted download is continued with a `Range` request instead of starting
      over.  Modelled on `cyberdrop-dl`'s split between a resume table and a hash table.
- [x] `osint-scraper history` to inspect the history and prune the entries whose file is gone
- [ ] HTML/CSV report per target (post table with engagement metrics)
- [ ] EXIF extraction and embedding (`piexif` or `Pillow`)
- [x] **Content-hash dedupe across platforms** — the `hashes` index is keyed on the SHA-256 alone,
      so the same image reposted on Instagram and Threads is stored once and the second copy is
      discarded in favour of the existing file
- [x] **Wired up the four config settings that were read by nobody** — each was declared in
      `config/default.yaml` and the settings dataclass but never consulted, so setting it was
      silently ignored:
      - `crawl.max_posts_per_target` — the "hard ceiling … so a runaway target cannot fill the
        disk" did not exist. It is now applied per target and the tighter of it and `crawl.limit`
        wins, with a warning when it bites.
      - `ratelimit.cooldown_minutes` — `ratelimit.py` applied its own constant. The setting now
        decides how long a throttled host is left alone.
      - `ratelimit.enabled` — cooldowns were always enforced. The switch now works: a throttle is
        logged but no cooldown is started, and an existing one is ignored.
      - `crawl.include_text_only` — text-only posts were always written to the manifest. The
        switch now decides, and defaults to keeping them so no existing run loses data.
- [x] **The default cooldown was 60 seconds, not 60 minutes.** `DEFAULT_COOLDOWN_MINUTES` was
      used as a seconds count, so a throttle paused the tool for one minute — sixty times shorter
      than the name claimed, and far too short for an account-level throttle (R11).
- [x] **A timeline cut short by the page ceiling now says so.** The GraphQL paging loop ran out
      of pages silently and the result was reported as a complete timeline. Every post now
      carries `truncated` and a `truncation_reason`, so a short history cannot be mistaken for
      a whole one.
- [ ] Packaging: AUR / Flatpak manifest / `pipx` entry point
- [ ] CI: `ruff`, `mypy --strict`, `pytest` on every push

## Upstream dependency watch

Meta breaks private endpoints on a **3–6 week** cadence. Both Instagram engines
are pinned; upgrade deliberately and keep a last-known-good version.

| Dependency | Pinned | Verified good | Latest known | Notes |
| --- | --- | --- | --- | --- |
| `instaloader` | 4.16 | 2026-10-30 | 4.16 (2026-10-26) | Active. 4.16 fixed `Profile.get_posts()` logged-in + `get_iphone_json()` |
| `gallery-dl` | 1.32.18 | — | 1.32.18 (2026-10-25) | Weekly releases. **Instagram extractor currently failing** (R3). Canonical source moved to Codeberg |
| `yt-dlp` | optional | 2026-10-30 | 2026.10.26 | Best keyring support of the three (`kwallet6`) |
| `httpx` | >=0.27 | 2026-10-30 | — | Core HTTP client |
| Threads UA | mobile-app UA | 2026-10-30 | — | **Not versioned** — a UA change would silently empty results (R5) |

## Risk register

| ID | Risk | Impact | Mitigation | Status |
| --- | --- | --- | --- | --- |
| R1 | Instagram breaks its private GraphQL | instaloader stops returning posts | Pin version; keep gallery-dl as an independent second extractor; pin a last-known-good | Open |
| R2 | Threads `doc_id` values rotate | GraphQL transport returns nothing | **Mitigated**: the query id and its variables are mined from the server-rendered page at run time, never hardcoded; the SSR page stays the fallback | Open |
| R3 | gallery-dl Instagram extractor failing today | Secondary engine unavailable | **Root cause found 2026-10-30**: gallery-dl mines its `doc_id` at run time, but fetches the profile with `Sec-Fetch-*` headers, and Instagram answers *that* request with a page carrying no JS bundles — so the mining finds nothing and the query id lookup raises `KeyError`. The same page fetched with a plain user agent carries six bundles and mines cleanly. Primary is instaloader; the mining technique is proven and can be implemented in-house | **Active** |
| R4 | Flatpak Chromium cookies are `v12` (SecretPortal) | Cookies unreadable for Flatpak-only users | **Resolved 2026-10-30**: `v12` is decryptable — the portal secret is persisted in the host keyring and a Secret Service client can read it. Implemented, with the detection path kept as the fallback when the secret cannot be found | **Closed** |
| R5 | Threads changes the User-Agent gate | SSR returns an empty shell | Multiple UA variants; loud `ParseError` with diagnostics; monitor the mobile UA | Open |
| R6 | Threads SSR pagination ceiling (~10–20 posts) | Incomplete history | **Resolved** by the GraphQL transport, with the SSR page kept as the fallback | Closed |
| R12 | The Threads GraphQL transport answers `1675030` for private profiles | Private Threads profiles stay unreadable | Nothing to do yet; the official Threads API needs app review and business verification, and only reads public profiles | Open |
| R7 | Rate limiting / account restriction | Collection stops | Jittered delays (never a fixed cadence); low request volume; unauthenticated posture by default | Mitigated |
| R8 | Meta ToS prohibits automated collection | Account or IP action | Scope to public content, document the legal basis per run, keep the collection log in the manifest | Open |
| R9 | Threads silently drops the post payload without the right `Accept` header | A 583 KB shell instead of the 960 KB payload, with **zero** posts and no error | Always send `Accept: text/html,application/xhtml+xml,*/*`; the crawler treats "no posts" as a `ParseError` with diagnostics | Mitigated |
| R10 | The keyring holds several entries under the same key | The wrong secret is picked and every `v11` cookie fails to decrypt | Collect every candidate and keep the one that actually decrypts | Mitigated |
| R11 | Instagram puts the *account* into a "try again later" throttle (`feedback_required`, `spam: true`) | Every authenticated call fails while anonymous calls keep working | Back off, use the tool's jittered delays, and treat a burst of `feedback_required` as a stop signal rather than retrying | **Active on the test account** |

## Decision log

| Date | Decision | Rationale |
| --- | --- | --- |
| 2026-10-30 | Threads discovery uses the **mobile-app UA SSR path**, not GraphQL | Verified working live (19 and 5 posts on two public profiles); GraphQL `doc_id` is the #1 breakage source |
| 2026-10-30 | Instagram primary engine is **instaloader**, not gallery-dl | instaloader 4.16 verified working unauthenticated; gallery-dl currently returns `NotFoundError` |
| 2026-10-30 | Extract posts by **structural signature**, not fixed JSON paths | A refactor of the envelope should degrade results, not zero them |
| 2026-10-30 | Chromium cookies are decrypted **in-process** instead of via `browser_cookie3` | `browser_cookie3` 0.20.1 needs `lz4`, which has no wheel for Python 3.15; the algorithm is small and now fully documented |
| 2026-10-30 | Cookies are exported to **Netscape `cookies.txt`** as the interchange format | One artefact works with instaloader, gallery-dl and yt-dlp; one place to fix when Chromium ships a new cipher tier |
| 2026-10-30 | Brave is the **first-class** browser | It is the operator's primary browser, system and Flatpak |
| 2026-10-30 | Architecture follows `cyberdrop-dl-patched` | Site-handler modules, YAML config, central cookie layer, SQLite history, manifest — but scoped to a handful of platforms instead of 245 file hosts |
| 2026-10-30 | The Threads crawler always sends `Accept: text/html,application/xhtml+xml,*/*` | Without it Threads returns a 583 KB shell with no post payload at all — a silent zero, not an error |
| 2026-10-30 | Every keyring candidate secret is tried and the one that decrypts wins | A KDE desktop holds two entries under the same key; only the `org.qt.keychain` one is the live key |
| 2026-10-30 | Engagement counts are read from the GraphQL node, never through `post.likes` / `post.comments` | Those properties trigger an *extra* metadata request per post, which fails outright on some private-account responses and doubles the request rate |
| 2026-10-30 | A throttle starts a **persistent** cooldown, and a run refuses to start during one | Retrying through a throttle is what turns a rate limit into a permanent spam flag |
| 2026-10-30 | Threads sends the merged `threads.net` + `threads.com` cookie header | The authenticated session lives on `.threads.com` but the site is served from `threads.net` |
| 2026-10-30 | Every browser profile is merged by default; `--profile`/`--source` narrow it | The session lives in one profile and the remaining cookies in the others, so merging beats guessing |
| 2026-10-30 | `csrftoken` is not counted as a session marker | It is present on any anonymous visit, so counting it would make an anonymous profile look authenticated |
| 2026-10-30 | A private Threads profile is reported as private, not as a parse failure | Threads does not server-render private profiles; the user object carries `text_post_app_is_private` |
| 2026-10-30 | An empty Threads payload is retried with a rotated user agent | Threads intermittently serves a smaller shell with no post objects; a retry with a different agent gets the payload |
| 2026-10-30 | The download history is keyed on the **URL path without its query string**, not the full URL | Instagram and Threads serve media from a CDN whose signature rotates every run, so a URL-keyed history would never match and a resume would re-download everything |
| 2026-10-30 | The history keeps a **media index and a content index** separately | `cyberdrop-dl` splits them the same way: one table says where a media is, the other says what content the library holds. The first drives the resume, the second the dedupe |
| 2026-10-30 | An existing file is **re-hashed** before it is trusted, and a mismatch re-downloads it | The platform can change the media under the same key; a size check alone would not notice |
| 2026-10-30 | A partial download is **continued with a `Range` request** instead of being discarded | An interrupted run should not throw away the bytes it already fetched |
| 2026-10-30 | The Threads GraphQL transport is **anonymous**, and the query id is mined from the page at run time | A logged-in request returns a different, non-paginating query, and the session cookie makes the endpoint answer with HTML instead of JSON. Mining the id means a bundle rotation is picked up automatically instead of breaking silently |
| 2026-10-30 | The **server-rendered page stays the fallback** for Threads | A broken GraphQL transport should degrade the result to the recent posts, not fail the run |
| 2026-10-30 | A target a platform does not recognise is **skipped for that platform**, not fatal | A Threads link used to die on the Instagram crawler before the Threads crawler ever saw it |
| 2026-10-30 | The cooldown length is expressed in **minutes**, and the constant is multiplied by 60 | `DEFAULT_COOLDOWN_MINUTES` was used as a seconds count, so a throttle paused the tool for one minute instead of an hour — sixty times too short for an account-level throttle |
| 2026-10-30 | The **cookie extraction cache** is keyed on the selection, and the store dir is `0700` | A cache written for one profile must never be served for another, or the tool would scrape with the wrong session; exported cookies are live sessions and were being written world-readable |
| 2026-10-30 | The Instagram surfaces are selected by the **target URL**, not by a flag | It matches how `yt-dlp` selects by URL and how the Threads crawler already handles a direct post link, and it needs no new CLI surface |
| 2026-10-30 | A carousel child's identity is read from the **parent's `carousel_media`** when it is there | instaloader's sidecar objects carry only a URL and a video flag. The parent's node carries the child's own media id, media type and dimensions — but only for anonymous reads, so an authenticated run gets what the platform gives it and no extra request is made |

## Explicitly not doing

- **Threads tokenless oEmbed as a data source.** The endpoint's own terms forbid consuming,
  extracting or persisting the metadata for anything other than a front-end view. It is an embed
  helper, not a scraping API.
- **snscrape.** Last commit 2023; the upstream fix for Instagram was never merged.
- **Login-wall circumvention, CAPTCHA solving, multi-account rate-limit evasion.** Out of scope.
- **Private, protected or follow-gated accounts.** The tool targets public content only.

## Open questions

1. Should Threads collections be exported with the **engagement metrics** that the unofficial path
   exposes (`like_count`, `direct_reply_count`), at the cost of a higher ToS exposure than the
   official API? Current answer: yes, because the official API does not expose them at all — but
   this needs a decision recorded per deployment.
2. Should the tool ever be able to run the **official Threads Graph API** (`graph.threads.com`) when
   the operator has App Review approval? It is ToS-clean but needs `threads_profile_discovery` and
   a profile with ≥100 followers.
3. Should search-engine discovery be opt-in per run, or a separate command that emits candidates
   for review before any scraping happens? (Leaning: separate command.)
4. Do we want a **flatpak manifest** for the tool itself, so it can read Flatpak browser profiles
   without path juggling?

## Session notes

Append a short entry per working session.

### 2026-10-30 — initial implementation

- Verified the environment: Python 3.15.2, SteamOS/KDE, Brave (system + Flatpak), Chrome, Edge and
  LibreWolf installed as Flatpaks, KWallet (`kwalletd6` + `ksecretd`) as the secret store.
- Verified `instaloader` downloads Instagram public profile media without a login (226 MB for one
  public profile); verified `gallery-dl` currently fails on the same target.
- Verified Threads returns full post data only when the request presents the **mobile-app
  User-Agent** *and* an explicit `Accept` header, and built the extraction around a structural
  signature.
- Verified cookie extraction from Brave (system + Flatpak) and Chrome (Flatpak), and reverse
  engineered the Chromium Linux `v10`/`v11` scheme including the 32-byte integrity prefix.
- **The Flatpak Brave profile named `Profile 1` is logged into Instagram and Threads** — it holds
  `sessionid` for both, plus `c_user`/`xs` for Facebook, and 1177 cookies decrypt from it. The
  `Default` profile is anonymous, so always pick the profile with the most cookies.
- Fixed three live bugs that unit tests could not have caught: the Threads login-wall false
  positive, the non-existent `instaloader.get_displayable_urls()`, and the `secret-tool`
  two-entry keyring lookup.
- End-to-end verification: 25 MB across both platforms in a single run, 42 manifest entries with
  SHA-256 digests, deterministic filenames and one metadata sidecar per post.
- **Verified the authenticated path on a private profile**: `private_account` yields 5 posts and 16
  media files (15 MB, carousels included) with the browser session, and raises
  `LoginRequiredException` without it.
- Built the adaptive pacer and the persistent cooldown after the account was throttled with
  `feedback_required` / `spam: true`. Verified that a throttle stops the *next* invocation too.
