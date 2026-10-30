# CLAUDE.md

Working notes for this repository: the operator's instructions, the decisions taken, and the
technical facts that were verified against the live platforms rather than read from docs.

Read this before changing anything. The "Verified behaviour" section below is the part that
cannot be re-derived from the code alone — each item cost a live request to establish.

## The project

`osint-scraper` — a CLI that discovers the posts on a target's social media profile and
downloads the media attached to them. Instagram and Threads are implemented; search engines and
Facebook are planned.

```bash
.venv/bin/osint-scraper scrape somehandle --platform instagram --platform threads --limit 5
```

Full status, pinned upstream versions and the risk register live in `docs/ROADMAP.md`. That file
is the tracker — update it whenever a milestone moves.

## Operator preferences and decisions

These were stated by the operator and are binding unless they say otherwise.

- **Brave is the primary browser.** Both the system install (`~/.config/BraveSoftware`) and the
  Flatpak (`~/.var/app/com.brave.Browser`) matter. Chrome, Chromium, Edge and Firefox are
  supported but are not the main target.
- **Instagram and Threads first.** Search engines and Facebook are deferred until those are
  solid. Do not start a new platform without asking.
- **Test against the live sites early.** A unit test cannot tell you that Instagram renamed an
  API method or that Threads requires an `Accept` header. Live-check anything that touches a
  platform's wire format.
- **Polite by default, and never retry through a throttle.** Retrying a throttled request is
  what turns a rate limit into a permanent spam flag. The cooldown system exists for this and
  must not be bypassed to "just get the data".
- **Run the full test suite, `ruff` and `mypy` before declaring anything done.** All three must
  be clean.
- **Do not commit or push.** The operator commits by hand so GPG signing works. Stage only.
- **A re-run must not download anything it already has.** The history is keyed on the media, not
  the URL: Instagram and Threads serve media from a CDN whose URL carries a short-lived signature,
  so the same image arrives under a different full URL every run. Key on the URL *path* without
  its query string. `cyberdrop-dl` is the reference for this split — its source is available
  read-only under `.slim/clonedeps/repos/Cyberdrop-DL__cyberdrop-dl/`.

## Verified behaviour

Every item here was established by a live request and confirmed by the operator's own machine.
Treat them as facts, not as guesses to re-litigate.

### Instagram

- **Public profile posts need no login.** `instaloader` 4.16 downloads them anonymously.
- **Stories, highlights, IGTV, tagged posts and private profiles need a session.** The browser's
  `instagram.com` cookies are handed to instaloader via `Instaloader.load_session()`.
- **`instaloader.get_displayable_urls()` does not exist in 4.16.** The working API is:
  `post._node` (the GraphQL dict) for `video_versions` and `image_versions2`; `post.typename` for
  `"GraphImage" | "GraphVideo" | "GraphSidecar"`; `post.get_sidecar_nodes()` for carousels.
  `post.media_type` and `post.node` do not exist either.
- **A video post's `image_versions2` is its poster frame**, not a second media item. Emit the
  video only.
- **`instaloader` needs `csrftoken` in the session dict** or `load_session()` raises `KeyError`.
- **Do not read `post.likes` or `post.comments`.** Those properties trigger an *extra* metadata
  request per post, which fails outright on some private-account responses (`KeyError:
  'edge_media_to_parent_comment'`) and doubles the request rate. The counts are already in the
  node: `edge_media_preview_like.count`, `fb_like_count`, `comment_count`, `fb_comment_count`.
- **A private profile is reachable with the session and not without.** Verified: authenticated
  yields the posts and their carousels, anonymous raises `LoginRequiredException`.
- **`gallery-dl` 1.32.18 is currently broken** for Instagram — `NotFoundError: Requested user
  could not be found`, from a `KeyError` on the query id. It is the intended secondary engine but
  cannot be relied on.
- **The root cause is gallery-dl's page fetch, not its mining.** gallery-dl already mines the
  `doc_id` at run time from the JS bundles — the same trick the Threads transport uses. But it
  requests the profile with `Sec-Fetch-Site: none` / `Sec-Fetch-Mode: navigate`, and **Instagram
  answers that request with a page carrying no JS bundle references at all** (778 KB, zero `.js`
  URLs). The same URL fetched with a plain user agent returns 512 KB with six bundles, and
  `PolarisProfilePageContentQuery` mines out of them with its doc id. So the technique is proven
  on Instagram too; only gallery-dl's headers are wrong.
- **The Instagram query id is mineable at run time, exactly like the Threads one.** From a plain
  user-agent profile page, the bundles under `static.cdninstagram.com/rsrc.php/` carry
  `__d("<Name>_instagramRelayOperation",[],…exports="<doc_id>")`. Verified:
  `PolarisProfilePageContentQuery` → a live doc id. The bundle URLs appear as both
  `<link href=…>` and `<script src=…>`, so a scraper must not look for only one.
- **`get_stories()` and `get_highlights()` live on the `Instaloader` class, not on `Profile`.**
  They are present in 4.16 and were never removed; only the `has_viewable_story` /
  `has_highlight_reels` *flags* are deprecated. `get_tagged_posts()` is **target**-scoped (the
  posts the target is tagged in), while `get_saved_posts()` is self-scoped and needs a login as
  that profile.
- **The Instagram surfaces are reached through the iPhone API**, and they work:
  - stories — `api/v1/feed/reels_media/?reel_ids=<userid>` → `reels[<userid>]["items"]`
  - highlights — `api/v1/highlights/<userid>/highlights_tray/` → `tray[]` (each entry's `id` is
    `"highlight:<numeric>"`, and its `items` list is empty), then per highlight
    `api/v1/feed/reels_media/?reel_ids=highlight:<numeric>` → the items
  - The web `api/v1/feed/reels_tray/` endpoint answers with **HTML** without the `Sec-Fetch-*`
    script headers, and instaloader 4.16 has no `get_web_api_json` — that is why the iPhone API
    is used instead.
- **A story or highlight item's `media_type` is 1 (image) or 2 (video).** A video item's
  `image_versions2` is its **poster frame**, not a second media object. Its `video_versions`
  holds three entries that share one URL and differ only in `type` (101 is the video);
  `101` is the one to take, with the highest bandwidth as the fallback.
- **IGTV is broken upstream**: `profile.get_igtv_posts()` raises `KeyError: 'edges'` because the
  GraphQL response shape changed. It is a generator, so the error surfaces during iteration.
- **Some instaloader accessors fetch eagerly.** `profile.get_posts()` and
  `profile.get_tagged_posts()` read the profile's metadata *before* returning an iterator, and
  building a post can fetch a post's full metadata when its node is partial. Both paths make
  network requests, so both have to sit inside the guard that translates a throttle into a
  `RateLimitError` — otherwise the raw instaloader exception escapes and no cooldown is
  registered.
- **A carousel child's identity lives in the parent's node, not in instaloader's child objects.**
  The parent carries a `carousel_media` list whose entries hold the child's own media id, media
  type and dimensions; instaloader's `get_sidecar_nodes()` hands back thin objects with only
  `display_url`, `video_url` and `is_video`. **The parent carries `carousel_media` only for
  anonymous reads** — an authenticated profile feed returns thin children instead, so an
  authenticated scrape yields the child's URL and kind but no media id and no dimensions. That is
  the platform's shape, not a bug in the crawler; fetching the post's full metadata would cost
  an extra GraphQL request per carousel, which the account-level throttle does not allow.
- **Instagram throttles per *account*, not per IP.** When it does, the response is HTTP 400 with
  `{"message": "feedback_required", "spam": true}` — while anonymous requests from the same IP
  keep working. That asymmetry is the tell.

### Threads

- **The desktop user agent returns an empty shell.** A profile page fetched with a desktop UA is
  ~258 KB with zero posts. The **mobile-app UA** returns ~960 KB with the full payload. The
  crawler sends `Threads 330.0.0.29.111 (iPhone14,2; iOS 17_5; ...)`.
- **An explicit `Accept: text/html,application/xhtml+xml,*/*` header is required.** Without it
  Threads returns a 583 KB shell with zero posts and *no error*. This is the single most
  dangerous failure mode: a silent zero rather than a crash.
- **There is no `__NEXT_DATA__`.** The payload lives in SvelteKit/Comet `data-sjs`
  `<script type="application/json">` blocks. Extract by **structural signature** — a dict with
  both `pk` and `code` plus one of `caption`, `image_versions2`, `video_versions`,
  `text_post_app_info` — never by a fixed JSON path. A refactor should degrade the results, not
  zero them.
- **The GraphQL transport works — but only anonymously.** `POST /api/graphql` with the
  `doc_id` and variables mined from the server-rendered page's `expectedPreloaders` manifest
  pages the whole timeline: **215 posts for `@somehandle` (2023-08-03 → 2026-10-13)** against 10
  through the page. `doc_id` rotates whenever Meta ships a bundle, so it must **never** be
  hardcoded — mining it from the page is what makes a rotation a non-event. Facts that cost
  live requests to establish:
  - The `doc_id` and the query's **exact variables** live in the page's
    `"expectedPreloaders"` array. Mining them means a bundle rotation is picked up automatically.
    Copy the variables **verbatim** — they carry ~50 feature flags that change with the bundle.
  - The request must be **anonymous**. A logged-in request returns a different, non-paginating
    variant of the query (one page, `has_next_page: false`), and the `sessionid` cookie makes
    the endpoint answer with **HTML instead of JSON**.
  - The cursor variable is **`after`**. `max_id`, `cursor` and `end_cursor` are silently ignored.
  - The endpoint serves JSON as **`text/javascript`**, so a content-type check is wrong — parse
    the body and treat a parse failure as an expired token.
  - The end of the timeline is **not** a clean stop: the last page fails with
    `code: 4686025, summary: "Post Unavailable"`. Treat it as "stop", never as an error.
- **Unauthenticated history is capped** at roughly the 10–20 most recent posts per profile.
- **Threads' history ceiling is a rendering limit, not an auth limit.** The server-rendered page
  returns the same ~10 posts with and without a session. The GraphQL transport (M2/R2) is the only
  way deeper, and it does work — anonymously.
- **A `/share/` link resolves through a 302** to the canonical post URL, which is where the
  author and the short code come from. The `/t/` short-link form does **not** redirect on
  `threads.net` — it serves an empty shell.
- **A post page carries the surrounding conversation too**, so the post has to be selected by its
  short code, not taken as the first one.
- **The Threads session cookie lives on `.threads.com` while the site is served from
  `threads.net`.** The crawler merges both domains into one `Cookie` header, because the cookie
  store scopes by domain.
- **Threads does not server-render private profiles.** A private account's page carries the user
  object — with `text_post_app_is_private: true` — but no posts at all. The crawler detects this
  and says the profile is private; do not let it be reported as a parse failure.
- **Threads intermittently serves an empty shell**, even with the right user agent and headers:
  a smaller response with the same number of JSON blocks but no post objects. Retry with a
  *rotated* user agent — repeating the same one can just repeat the failure.
- **Never synthesise `view_count = 0`.** It is absent on most posts; `like_count` and
  `direct_reply_count` are always present.
- **Quoted posts belong to another account.** They are nested in the parent's
  `share_info.quoted_post` and also match the post signature, so the walker finds them twice.
  Attach their media to the quoting parent; never emit them as top-level posts.
- **The payload can carry `"user": null`.** A quoted post whose author was deleted has the key
  present with a null value, so `dict.get("user", {})` returns `None` and the next `.get()` on it
  raises `AttributeError`. Every nested lookup goes through `_as_dict()`.

### Browser cookies (Linux)

- **Chromium `v10`**: `PBKDF2-HMAC-SHA1(password="peanuts", salt="saltysalt", 1, 16)` →
  AES-128-CBC, IV = 16 spaces, PKCS#7.
- **Chromium `v11`**: same derivation but the password comes from the keyring.
- **The decrypted plaintext starts with a 32-byte integrity prefix** that must be stripped.
  Skipping it yields plausible-looking garbage — this is the classic "why won't it decrypt" bug.
- **`v12` (Flatpak SecretPortal) is decryptable.** The old note in this file said it was not,
  which was wrong. `v12` is Chromium's `os_crypt_async` SecretPortal scheme: HKDF-SHA256 with
  salt `fdo_portal_secret_salt` and info `HKDF-SHA-256 AES-256-GCM` into a 32-byte key, then
  **AES-256-GCM** with a 12-byte nonce and a 16-byte tag, behind the same 32-byte integrity
  prefix as v10/v11. The portal secret is persisted in the **host** keyring (KWallet folder
  `xdg-desktop-portal`, entry named after the Flatpak app ID) and a Secret Service client can
  read it — `secret-tool` refuses only because the entry is **binary**.
- **`v12` is not Flatpak-exclusive in principle**, and the encryption flag is off by default in
  current Chromium, so most machines have no `v12` cookies at all.
- **Two pieces of advice that were wrong:** `chrome://flags` has no `#password-store-basic`
  option, and `flatpak override --env=CHROME_PASSWORD_STORE=basic` cannot work because the
  switch is argv-only. The real remediation is to launch the browser with `--password-store=basic`
  as a command-line switch; **no profile deletion is needed**, only re-authentication.
- **A desktop can hold several keyring entries under one key.** On this KDE machine the plain
  `secret-tool lookup server "Brave Keys" user "Brave Safe Storage"` returns a *stale* secret
  while the same lookup plus `xdg:schema org.qt.keychain` returns the live one. Collect every
  candidate and keep the one that actually decrypts.
- **Firefox stores cookie values in plaintext.** Firefox 147+ also uses
  `$XDG_CONFIG_HOME/mozilla/firefox/`.
- **`browser_cookie3` is not a dependency.** Its `lz4` requirement has no wheel for Python 3.15,
  and the algorithm is small enough to implement directly.

### Environment

- SteamOS, KDE Plasma, Python 3.15.2, pipx available, `uv` not installed.
- Keyring: KWallet via `ksecretd`; `secret-tool` works. `kwallet-query5` is not installed.
- Flatpak browsers present: Chrome 156, Brave 1.98, Edge 156, LibreWolf 158.
- **Cookie selection merges every profile by default.** The session is usually in one profile and
  the rest of the cookies in the others. `--profile` narrows it; a bare name that exists in both
  installations is refused rather than guessed. `csrftoken` is **not** a session marker — it is
  present on any anonymous visit.
- The logged-in browser profile is Flatpak Brave **"Profile 1"** (1181 cookies, `sessionid` for
  Instagram and Threads). `Default` is anonymous.

## Coding standards

The operator's `python` skill applies: PEP 8, **ruff** for lint and format, **mypy strict**,
type hints on every signature, `from __future__ import annotations`, `pathlib.Path` over
`os.path`, **never a bare `except:`**, custom exception classes for domain errors,
`logger.exception(...)` for tracebacks, dataclasses for data holders, small single-purpose
functions.

Markdown follows the `markdown` skill: one H1, no skipped heading levels, ATX headings only, fenced
code blocks with a language tag, pipe tables with matching column counts, descriptive link text,
120-character prose lines.

## Git

- **Never run `git commit` or `git push`.** The operator commits by hand so GPG + TTY work.
  Stage with `git add` and hand them the message.
- Conventional Commits: `type(scope): subject`, imperative, lowercase, subject ≤ 50 characters.
- No AI attribution trailers. No filler openers.
- Fetch first, check divergence, then stage.

## Layout

```text
src/osint_scraper/
├── cli.py               # scrape / cookies / cooldown / history / doctor / platforms
├── config/settings.py   # layered settings: defaults -> YAML -> CLI
├── cookies.py           # browser discovery, Chromium/Firefox decryption, cookies.txt
├── crawlers/
│   ├── base.py          # Crawler ABC, CrawlContext, registry
│   ├── instagram.py     # instaloader
│   ├── threads.py       # Threads crawler: GraphQL timeline, single posts, SSR fallback
│   └── threads_api.py   # the Threads transport: user agents, GraphQL session, its pages
├── downloader.py        # concurrent, paced, hashed downloads; resume, file check, dedupe
├── database.py          # SQLite history: media index, content index, seen posts
├── manifest.py          # JSONL audit trail
├── models.py            # Post / MediaRef / ProfileInfo — the shared contract
├── ratelimit.py         # adaptive pacing + persistent cooldowns
└── filepath.py          # sanitisation, extension guessing
```
The boundary that matters: **a crawler discovers, the downloader transports.** A crawler never
writes files; the downloader never performs discovery. Adding a platform means implementing the
crawler contract only.

## Resume and the history

`database.py` holds two indexes and they answer different questions:

- `media` — *where did this media end up?* Keyed on `(platform, media_key)` where `media_key` is
  the URL path without its query string. This drives the resume.
- `hashes` — *do we already have this content?* Keyed on the SHA-256. This drives the dedupe.

The rules, in order of importance:

1. **Key on the media, never the full URL.** The CDN signature rotates on every run.
2. **The path has to be stable.** With the resume on, the destination is derived from the post's
   timestamp and shortcode — the same media must land on the same name every run, or the history
   can never find it again. Without the resume the old non-clobbering behaviour applies.
3. **Hash before you trust a file.** A file whose digest no longer matches the record is
   re-downloaded: the platform changed the media. A deleted file is re-downloaded too.
4. **A `.part` file is continued, not discarded.** The next run sends `Range: bytes=N-`. If the
   server ignores the range it is restarted cleanly; a `416` means the partial is stale and is
   thrown away.
5. **The counters partition the media.** `downloaded + skipped + failed` is the number of media
   items. `duplicates` and `resumed` are markers within those.

`osint-scraper history` shows the state; `osint-scraper history --prune` drops the rows whose
file is gone.

## Cooldowns

`ratelimit.py` owns the pacing. The rules, in order of importance:

1. **A fixed interval is a signature.** The gap between requests to one host is
   `random.uniform(delay, delay + jitter)`, never a constant.
2. **A throttle doubles the delay.** Every consecutive throttle multiplies the gap, up to 16×. It
   relaxes back to the baseline as soon as the host stops pushing back.
3. **The cooldown is persistent.** It is written to
   `~/.cache/osint-scraper/cooldown.json`, so the *next* invocation honours it. A run refuses to
   start while a host is cooling down.
4. **Never retry through a throttle.** `RateLimitError` is the answer; the fix is to wait.

`osint-scraper cooldown` shows the state; `osint-scraper cooldown --clear` overrides it
deliberately.

## Collected identifiers

`docs/TARGETS.md` records **what the cookie subsystem can reach** — which cookie carries the
session, which browser profile holds it, which keyring entry actually decrypts. The handles and
numeric account ids themselves are deliberately **not** in the repository: a repo is not the place
to keep the operator's own identifiers. Keep it that way when new identifiers are collected —
record the structure, not the names.
