# Browser cookies

Cookies let the tool reach content that is not public: Instagram stories and highlights,
follow-gated posts, and the deeper Threads history that the anonymous profile page does not
expose.

Cookies are read from **your own browser, on your own machine**. Nothing is uploaded, and cookie
values are never logged or printed.

## Quick start

```bash
# See what is available
osint-scraper cookies list

# Export from Brave (the default)
osint-scraper cookies export --browser brave --output ./brave-cookies.txt

# Check whether the exported cookies contain a logged-in session
osint-scraper cookies export --browser brave --check
```

The export is a **Netscape `cookies.txt`** file. That format is accepted by every consumer in
this ecosystem — `instaloader --cookiefile`, `gallery-dl --cookies` and `yt-dlp --cookies` — so
one artefact works everywhere.

## Which profile?

Browsers keep several profiles, and the logged-in one is usually not the one named `Default`.
`osint-scraper cookies list` shows every profile, what it holds, and the spec to pass:

```console
$ osint-scraper cookies list --browser brave
```

| Field | Meaning |
| --- | --- |
| `spec` | The source-qualified identifier, e.g. `flatpak:Profile 1` |
| `cookie_count` | How many cookies the profile holds |
| `session_domains` | The domains it holds an **authenticated** session for |

**By default every profile is used and their cookies are merged.** The session lives in one
profile and the rest of the cookies in the others; on a conflict the profile with the stronger
session wins. Narrow it when you need to:

```bash
osint-scraper scrape target --profile "Profile 1"          # unique name
osint-scraper scrape target --profile "flatpak:Profile 1"  # unambiguous
osint-scraper scrape target --source flatpak               # one installation
```

A bare name that exists in both installations is refused rather than guessed:

```text
error: profile 'Default' is ambiguous for brave (it exists in flatpak, system).
Qualify it: flatpak:Default, system:Default
```

`csrftoken` is **not** counted as a session: it is present on any anonymous visit, and treating
it as a session would make an anonymous profile look authenticated.

## Where cookies live

| Browser | System install | Flatpak install |
| --- | --- | --- |
| Brave | `~/.config/BraveSoftware/Brave-Browser/<Profile>/Cookies` | `~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/<Profile>/Cookies` |
| Chrome | `~/.config/google-chrome/<Profile>/Cookies` | `~/.var/app/com.google.Chrome/config/google-chrome/<Profile>/Cookies` |
| Chromium | `~/.config/chromium/<Profile>/Cookies` | `~/.var/app/org.chromium.Chromium/config/chromium/<Profile>/Cookies` |
| Edge | `~/.config/microsoft-edge/<Profile>/Cookies` | `~/.var/app/com.microsoft.Edge/config/microsoft-edge/<Profile>/Cookies` |
| Firefox | `~/.mozilla/firefox/<profile>/cookies.sqlite` | — |

`<Profile>` is a directory such as `Default` or `Profile 1`. Firefox 147 and newer also use
`$XDG_CONFIG_HOME/mozilla/firefox/<profile>/cookies.sqlite`; both locations are checked.

When no profile is named in the config, the tool picks the profile that holds the **most**
cookies, preferring the Flatpak copy of the browser over the system one.

## How Chromium cookies are encrypted

Chromium-family browsers store cookie values encrypted in the `encrypted_value` column of the
`cookies` SQLite table. The scheme is identified by a three-byte prefix.

| Prefix | Scheme | Key source |
| --- | --- | --- |
| `v10` | AES-128-CBC, key = `PBKDF2-HMAC-SHA1(password="peanuts", salt="saltysalt", 1, 16)` | none — the password is fixed |
| `v11` | AES-128-CBC, same derivation | the OS keyring secret for the browser |
| `v12` | xdg-desktop-portal SecretPortal | not readable by third-party tools |
| `v20` | App-Bound Encryption | not readable outside the browser process |

All of them use an IV of sixteen space characters and PKCS#7 padding. Two details matter:

1. **The plaintext carries a 32-byte integrity prefix** (a keyed digest of the cookie) that has
   to be stripped before the value. Skipping this step yields plausible-looking garbage.
2. **`v11` cookies additionally need the keyring secret**, and an empty-password variant exists
   for cookies written during a historical keyring race in Chrome ~89.

### Getting the keyring secret

On Linux the secret lives in the desktop's secret store. This tool reads it through `secret-tool`
and tries every layout and every candidate entry that is in the wild:

```bash
# KWallet native layout, with the Qt keychain schema
secret-tool lookup server "Brave Keys" user "Brave Safe Storage" xdg:schema "org.qt.keychain"

# KWallet native layout, without the schema
secret-tool lookup server "Brave Keys" user "Brave Safe Storage"

# Secret Service layout
secret-tool lookup xdg:schema chrome_libsecret_os_crypt_password_v2
```

The product names are `Brave`, `Chrome`, `Chromium` and `Microsoft Edge`.

**A desktop can hold more than one entry under the same key.** On the KDE machine this was
developed on, the keyring holds two `Brave Safe Storage` entries, and the plain lookup returns
the *stale* one while the lookup that adds `xdg:schema org.qt.keychain` returns the live key.
Picking the first hit would silently fail to decrypt every `v11` cookie. This tool therefore
collects all candidates and keeps whichever one actually decrypts — a wrong key simply fails the
padding check and the next candidate is tried.

If `secret-tool` is missing, install it — on Arch that is `libsecret-tools`, on Debian
`libsecret-tools`, on Fedora `libsecret`. A missing `secret-tool` produces a misleading
"system keyring unavailable" error even when the keyring itself is running fine.

## The `v12` problem

Flatpak-packaged Chromium browsers cannot reach the host keyring directly, so they go through the
xd-desktop-portal SecretPortal, which stores the cookie key under the `v12` scheme. **No
third-party tool can decrypt `v12` cookies** — `browser_cookie3`, `gallery-dl`, `yt-dlp`,
`pycookiecheat` and `rookie` all fail on them.

When this tool detects `v12` cookies it says so explicitly instead of silently returning
nothing. The fix is on the browser side: start the Flatpak browser with `--password-store=basic`
so it writes `v10` cookies, which are decryptable by everyone:

```bash
flatpak run --command=google-chrome com.google.Chrome --password-store=basic
```

Log in to the site while it is running in that mode, then export the cookies again.

### A note on `browser_cookie3`

The obvious dependency for this job, `browser_cookie3`, has two problems: its last release was
2025-01-04, and it depends on `lz4`, which has no wheel for Python 3.15 and fails to build from
source. The decryption scheme is small and fully documented above, so this tool implements it
directly instead.

## Firefox

Firefox stores cookie values in plaintext in `cookies.sqlite`. Nothing needs decrypting. The only
caveat is that Firefox 147 moved its profile root to the XDG config directory.

## Detecting a logged-in session

The export reports whether the collected cookies contain an authenticated session:

| Site | Session cookie |
| --- | --- |
| Instagram | `sessionid` |
| Threads | `sessionid` (plus `csrftoken`) |
| Facebook | `c_user` and `xs` |

A browser that has merely *visited* a site will have `csrftoken`, `datr`, `ig_did` and friends,
but no `sessionid` — that is an anonymous session, and it will not unlock gated content. The
tool tells you which case you are in so the difference is never a mystery.

When no profile is named in the config, the tool uses **every** profile of the browser and merges
their cookies, preferring the profile with the strongest session when two hold the same cookie.
You can pin a specific profile with `cookies.profile` in the config, or on the command line with
`--profile "flatpak:Profile 1"`.

## Safety notes

- Exported cookie files contain live session credentials. Keep them at mode `0600`, store them
  outside your media collection, and delete them when the session is no longer needed.
- The tool never prints cookie values — only names, counts and domains.
- If a browser is running while you export, the database is copied first, so a live browser can
  never corrupt the read.
