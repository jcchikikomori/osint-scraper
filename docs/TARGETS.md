# Collected identifiers

What the cookie subsystem is able to reach, observed on the operator's own machine on
2026-10-30. This is the reference set for the authenticated and private-profile tests.

**The handles and the numeric account id are redacted.** A repository is not the place to keep
the operator's own identifiers, and the live session matters more than the name of the account
it belongs to. The structure below — which cookie carries the session, which profile holds it,
which keyring entry actually decrypts — is what the tool depends on, and none of it is
personal.

## Operator's own account

| Platform | Handle | Numeric id | Session |
| --- | --- | --- | --- |
| Instagram | *redacted* | *redacted* | `sessionid` present |
| Threads | *redacted* | same | `sessionid` present on `.threads.com` |

The Instagram account is **private** — it is the target of the authenticated test. The
handle and the numeric id are deliberately not recorded here: this file is the operator's
working reference, and a repository is not the place to keep them.

## Cookie inventory — `.instagram.com`

| Cookie | Encrypted length | Role |
| --- | --- | --- |
| `sessionid` | 115 | **Authenticated session.** The one that matters. |
| `csrftoken` | 83 | Required by `instaloader.load_session()`; a missing token raises `KeyError`. |
| `ds_user_id` | 51 | The numeric account id. |
| `mid` | 67 | Device/session id. |
| `ig_did` | 83 | Device id. |
| `datr` | 67 | Device id, anonymous. |
| `ig_nrcb` | 51 | Anonymous. |
| `ps_l`, `ps_n` | 51 each | Anonymous. |
| `rur` | 147 | Anonymous. |
| `wd` | 51 | Anonymous. |

## Cookie inventory — Threads

`.threads.com` carries `sessionid`, `csrftoken`, `ds_user_id`, `mid`, `ig_did`, `ps_l`, `ps_n`.
`.threads.net` carries only `ig_did`, `ps_l`, `ps_n` — the authenticated session lives on
`.threads.com`, and the crawler talks to `threads.net`. The cookie store scopes cookies by
domain, so the Threads crawler **merges both domains into one `Cookie` header** before every
request. Without that merge the Threads request would carry no session at all.

`www.threads.com` additionally carries `cb`.

## Browser profiles

| Browser | Source | Profile | Cookies | Instagram session |
| --- | --- | --- | --- | --- |
| Brave | system | `Default` | 9 | no |
| Brave | flatpak | `Default` | 258 | no |
| Brave | flatpak | **`Profile 1`** | **1171** | **yes** |
| Chrome | flatpak | `Default` | 258 | no (`sessionid` absent) |
| Chrome | flatpak | `Guest Profile` | — | — |
| Chrome | flatpak | `Profile 1` | — | — |
| Chromium | system | `Default` | 7 | no |
| Edge | flatpak | — | 147 | — |

## Keyring entries

The KDE keyring holds **two** entries for `Brave Safe Storage`:

| Entry | Attributes | Value |
| --- | --- | --- |
| Live | `server=Brave Keys`, `user=Brave Safe Storage`, `xdg:schema=org.qt.keychain` | decrypts the `v11` cookies |
| Stale | `server=Brave Keys`, `user=Brave Safe Storage` (no schema) | does **not** decrypt |

Both are collected by the tool and the one that actually decrypts is kept. See
`docs/COOKIES.md`.

Other entries present: `Chromium Keys/Chromium Safe Storage`, `Slack Keys/Slack Safe Storage`,
`Secret Service/Chrome Safe Storage Control` (Chrome's dummy unlock entry).

## Instagram account status

During heavy testing the account was returned this state:

```json
{"message": "feedback_required", "spam": true, "feedback_title": "Try Again Later"}
```

That is Instagram's **account-level** throttle — anonymous requests from the same IP kept working
while every authenticated call was rejected. It is recorded as risk R11 in the tracker, and it is
the reason the cooldown system exists.

The throttle cleared on its own, and the private profile was then scraped successfully:

| Mode | Result |
| --- | --- |
| Authenticated | 5 posts, 16 media files, 15 MB, carousels included |
| Anonymous | `LoginRequiredException: Login required to access the posts of private profile <handle>.` |

That pair is the proof that the session — not luck — is what makes the difference.

## Threads equivalents

| Profile state | Result |
| --- | --- |
| public, active account | 215 posts over the GraphQL transport; 10 through the server-rendered page |
| public, low-volume account | 5 posts, 5 captions |
| **private** (`text_post_app_is_private: true`) | user object present, **0 posts** |

Threads does not server-render private profiles, so a private account yields no posts on
`threads.net` even with a valid session. The crawler reports that explicitly rather than
failing as a parse error.

The same account *is* reachable on Instagram, where the private profile's posts and carousels
come back normally with the session.
