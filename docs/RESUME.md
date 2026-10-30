# Re-running, resuming and the output layout

A second run of the tool does **not** download the same media again. This is the part of the
tool that decides what is new, and it is the part that has to be right about identity.

## Contents

- [The history database](#the-history-database)
- [Three mechanisms](#three-mechanisms)
- [Content that is already in the library](#content-that-is-already-in-the-library)
- [The counters](#the-counters)
- [Output layout](#output-layout)
- [The manifest](#the-manifest)
- [The metadata sidecar](#the-metadata-sidecar)

## The history database

Every download is recorded in a SQLite database, by default at
`~/.cache/osint-scraper/history.db`. It holds three tables:

| Table | Answers | Key |
| --- | --- | --- |
| `media` | *Where did this media end up?* | `(platform, media_key)` |
| `hashes` | *Do we already have this content?* | the SHA-256 digest |
| `posts` | *Have we seen this post?* | `(platform, post_id)` |

The split between `media` and `hashes` is deliberate and is the same split `cyberdrop-dl`
uses: one table says where a media item is, the other says what content the library holds.
The first drives the resume, the second drives the dedupe.

The history lives **outside** the output directory on purpose. Deleting the media does not
delete the knowledge that it was there, and `osint-scraper history --prune` is how you tell
the tool that.

```bash
osint-scraper history            # what is recorded
osint-scraper history --prune    # drop the rows whose file is gone
```

## Three mechanisms

### Stable media key

Instagram and Threads serve media from a CDN whose URL carries a short-lived signature. **The
same image arrives under a different URL every run**, and sometimes from a different CDN host.
A history keyed on the full URL would therefore never match, and a resume would download
everything again.

The history is keyed on the **URL path without its query string**. That identifies the media
itself and stays stable across runs.

### Hash check

Before an existing file is trusted, its SHA-256 is compared with the recorded digest.

| On disk | Recorded digest | Result |
| --- | --- | --- |
| Present, digest matches | matches | Skipped — nothing is fetched |
| Present, digest differs | differs | Re-downloaded — the platform changed the media |
| Missing | — | Re-downloaded |
| Unchanged size | not compared | Only with `--no-verify` |

A size check alone would not notice the platform changing the media under the same key, so the
digest is what is compared. `--no-verify` skips the re-hash and compares sizes only, which is
much cheaper on a large library and correspondingly weaker.

### Partial resume

An interrupted download leaves a `.part` file behind. The next run sends a `Range` header
asking for the remainder instead of starting over.

- If the server honours the range, the download continues.
- If the server ignores it, the partial is discarded and the file is fetched cleanly.
- A `416` means the partial is stale — the server has something longer — and it is thrown away.

## Content that is already in the library

Media whose content is already stored under a different name is **not** stored twice. The
freshly downloaded bytes are compared against the content index; if the digest is already
known, the new file is removed and the record points at the copy that already exists.

Because the content index is keyed on the digest alone, this works **across platforms**: the
same image reposted on Instagram and Threads is kept once.

## The counters

The download summary partitions the media items:

```console
$ osint-scraper scrape somehandle --platform threads
{
  "downloads": { "downloaded": 0, "skipped": 2, "duplicates": 0, "resumed": 0, "failed": 0 }
}
```

| Counter | Meaning |
| --- | --- |
| `downloaded` | Bytes fetched and stored |
| `skipped` | Media that was already present, or that was discarded as a duplicate |
| `failed` | Media that could not be fetched |
| `duplicates` | A subset of `skipped` — the content was already in the library |
| `resumed` | A subset of `downloaded` — the transfer continued a partial file |

`downloaded + skipped + failed` is the number of media items the run found. `duplicates` and
`resumed` are markers within those, not additional items.

## Output layout

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

Files are named from the post's **timestamp** and **shortcode**, so the same media lands on the
same name every run. That stability is what lets the history find it again. Without the resume
enabled, the old non-clobbering naming applies instead and a second run will not overwrite.

## The manifest

`manifest.jsonl` holds one JSON object per crawled post, one per **media object** attached to
a post, and one per downloaded file. It is the audit trail of a run, and it is append-only.

### The `post` row

```json
{"kind":"post","platform":"threads","target":"somehandle","post_id":"4006434140014576097","url":"https://...","timestamp":1758000000,"metadata":{"shortcode":"DfCjvSCEbct","caption":"…","media_count":1,"saved":1}}
```

### The `media` row

One per media object. A carousel used to be indistinguishable from three unrelated posts: every
child shared the parent's `post_id`, and its only identity was a `_1` / `_2` / `_3` filename
suffix. The `media` row gives each child its own identity.

```json
{"kind":"media","platform":"instagram","target":"somehandle","post_id":"4011374380604567319","url":"https://...","timestamp":1729958400,"metadata":{"index":0,"media_id":"4016734873234909654_787132","kind":"image","width":1080,"height":1350,"mime":"image/jpeg","extension":".jpg","shortcode":"DerQ775"}}
```

| Field | Meaning |
| --- | --- |
| `index` | The object's position in the post's media list, zero-based |
| `media_id` | The platform's own identifier for the object, when it exposes one |
| `kind` | `image`, `video`, `audio` or `unknown` |
| `width`, `height` | Pixel dimensions, when known |
| `mime`, `extension` | The media type and the file extension used |

The `media` row records what was **discovered**; the `file` row records what was **stored**.

### The `file` row

```json
{"kind":"file","platform":"threads","target":"somehandle","post_id":"4006434140014576097","url":"https://...","path":"threads/somehandle/2026-10-17_20-00-01_DfCjvSCEbct.mp4","sha256":"…","size":284913,"timestamp":1758000000}
```

`path` is relative to the output root. `sha256` is the digest of the bytes on disk, and it is
the same value the content index is keyed on.

## The metadata sidecar

Alongside each post's media, the tool writes a `.json` sidecar describing the post. It gathers
the same information as the manifest rows into one file per post, which is convenient to read
with a text editor or to feed to another tool.

```json
{
  "platform": "instagram",
  "target": "somehandle",
  "post_id": "4011374380604567319",
  "shortcode": "DerQ775Ca8XDCVVjm_Qc5XAlbw2g9icVp-7F7Y0",
  "permalink": "https://www.instagram.com/p/DerQ775Ca8XDCVVjm_Qc5XAlbw2g9icVp-7F7Y0/",
  "caption": "…",
  "timestamp": 1792384311,
  "media": [],
  "metadata": { "like_count": 34, "typename": "GraphSidecar", "owner_username": "somehandle" },
  "quoted_post_id": null,
  "parent_post_id": null,
  "files": [
    {
      "url": "https://…",
      "path": "instagram/somehandle/2026-10-19_04-31-51_DerQ775….jpg",
      "sha256": "…",
      "size": 1059358,
      "kind": "image",
      "skipped": false,
      "duplicate": false
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `media` | The media objects the post carried, before the download |
| `metadata` | Platform extras — engagement counts, the owner, the surface |
| `quoted_post_id`, `parent_post_id` | Set when the post quotes or replies to another |
| `files[].kind` | `image`, `video`, `audio` or `unknown` |
| `files[].skipped` | The file was already present and was not fetched again |
| `files[].duplicate` | The content was already in the library under another name |

Sidecars are written when `download.save_metadata` is true, which is the default.
