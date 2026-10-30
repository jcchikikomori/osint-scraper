# Disclaimer and legal notice

This document explains what `osint-scraper` does, what it deliberately does not do, and the
legal and ethical responsibilities that come with running it. It is part of the software: read
it before you collect anything, and keep it with any copy of the tool you share or deploy.

**This is not legal advice.** It is an engineering document written by the people who built the
tool, describing the tool's behaviour and the risks its operators take on. It cannot know your
jurisdiction, your sector, your data subjects or your purpose. For anything commercial, anything
involving personal data about identifiable people, or anything where the answer is not obvious,
get advice from a qualified lawyer in your jurisdiction before you collect.

## Contents

- [What the tool does](#what-the-tool-does)
- [What the tool deliberately does not do](#what-the-tool-deliberately-does-not-do)
- [Your responsibilities](#your-responsibilities)
- [Terms of Service](#terms-of-service)
- [The law](#the-law)
- [Copyright](#copyright)
- [Data protection and privacy](#data-protection-and-privacy)
- [Session cookies are credentials](#session-cookies-are-credentials)
- [Acceptable use](#acceptable-use)
- [No warranty](#no-warranty)
- [Limitation of liability](#limitation-of-liability)
- [Indemnity](#indemnity)
- [Governing law](#governing-law)
- [Third-party components](#third-party-components)
- [Reporting a concern](#reporting-a-concern)

## What the tool does

`osint-scraper` automates a task a person can do by hand in a browser: it opens a social media
profile, reads the posts that profile shows, and downloads the images and videos attached to
them. It writes a JSONL manifest recording what was found, where it came from and when.

The relevant behaviours are these, and they matter for everything below:

- **It reads profiles.** A profile, a single post, and — for Instagram — the surfaces of a
  single account: the posts it is tagged in, its active story, and its highlights.
- **It is anonymous by default.** Public posts need no credentials. With `--no-cookies` the
  tool never touches your browser's cookie store.
- **With `--cookies` it borrows your own browser session.** It reads the cookies of the
  browsers installed on your machine and presents them as you. It does not create accounts,
  does not share credentials between people, and does not use anyone else's session.
- **It is polite by construction.** Request pacing is jittered, a throttle starts a persistent
  cooldown that survives across runs, and the tool refuses to start while a cooldown is in
  force. There is no code path that retries through a rate limit.
- **It keeps an audit trail.** The manifest records every post, every media object and every
  downloaded file, with the target, the timestamp and the SHA-256 digest of the bytes.

## What the tool deliberately does not do

These are not omissions. They are design decisions, and they are the reason the tool is as
safe to run as it is:

- **No login-wall circumvention.** It does not solve CAPTCHAs, does not bypass challenges, and
  does not defeat an access control it does not have the credentials for.
- **No multi-account evasion.** There is no account rotation, no proxy pool, no randomised
  identity. A throttle is answered by waiting.
- **No credential sharing.** The session is the one already on your machine, for your own
  account.
- **No tracking of people who are not the target.** A run produces one target's posts. It does
  not follow mentions, build a follower graph or enumerate who interacted with the target.
- **No hidden identifier collection.** The repository records no handles, no numeric account
  ids and no session metadata, and the operator's own identifiers were deliberately removed
  from it.

## Your responsibilities

Running this tool is an act of **collection**, and collection carries obligations that the
software cannot discharge for you. Before a run, you should be able to answer these:

1. **Am I authorised?** Either the content is public and I have a lawful basis to collect it,
   or I have the subject's consent, or I have permission from the account holder. "It was
   visible" is not authorisation.
2. **Do I have a lawful basis?** If the content is personal data about an identifiable person,
   you need one under the applicable data protection law, and you need to be able to document
   it.
3. **Do I need to notify someone?** Many regimes require a privacy notice to data subjects,
   or a registration with a regulator, before collecting personal data at scale.
4. **How long will I keep it, and who can see it?** Collection without a retention limit and an
   access control is a liability, not a dataset.
5. **Can I show my work?** The manifest is the record of what you collected, from where and
   when. Keep it with the collection. It is the strongest evidence that a run was scoped,
   deliberate and proportionate.

## Terms of Service

Meta's Terms of Service — which cover Instagram and Threads — prohibit automated means of
collecting data from the services without permission. The `robots.txt` file published for
Threads disallows crawling. The same is broadly true of every platform this tool touches.

This matters in practice, not just on paper:

- Your **account** is the thing that gets actioned. A private profile's owner has no ability to
  object to their own data being collected; a platform's user can.
- The **account-level throttle** the tool reacts to is the platform's response to
  automated access. It is a signal to slow down, not to try harder.
- A platform's terms are a **contract between you and the platform**. Breach of them is not
  automatically a crime, but it can end your account and it can be relied on in a dispute.

Running this tool against a platform whose terms forbid it may breach those terms. That is a
decision you make, with your own name on it.

## The law

Two decisions from the United States are the ones most often cited, and it is worth being
precise about what they do and do not say.

**`hiQ Labs, Inc. v. LinkedIn Corp.`, 31 F.4th 1180 (9th Cir. 2022)** held that scraping
publicly available data did not breach LinkedIn's User Agreement, and that public data is not
"goods" within the Computer Fraud and Abuse Act. The holding is narrow. It concerns public
data and contract claims on the Ninth Circuit's facts, and it says nothing about private data,
other jurisdictions, other causes of action, or the platform's own intellectual property.

**`Van Buren v. United States`, 593 U.S. 374 (2021)** narrowed the CFAA's "exceeds authorized
access" to mean accessing a computer without authorisation — the gates-up-or-down question —
rather than accessing permitted information for improper purposes. Where a ToS prohibits
scraping, that prohibition is generally not by itself a CFAA violation. "Generally" is doing
real work in that sentence: authorisation questions under other statutes, state computer-crime
laws, contract claims and platform intellectual property all remain live.

**`hiQ` does not protect you from:**

- **Contract claims** outside the Ninth Circuit, or outside its facts.
- **Copyright** claims. See below.
- **Database rights.** The EU Database Directive and the UK equivalent create a sui generis
  right in the contents of a database, separate from copyright in the individual items.
- **Privacy and data protection claims.** These are the most likely claim you will actually
  face, because the people in the scraped content did not agree to it.
- **Any jurisdiction other than the United States.** The law here is not the law where you are.

## Copyright

**The media you download is almost always owned by someone else.** A photograph, a video, a
screenshot and a caption are protected works. Downloading them is a reproduction, and
reproducing a protected work is the act that copyright restricts, whether you then publish it
or keep it privately.

Copyright in user-generated social media content is real but thin. A photograph is thin
copyright; an original photograph uploaded by its creator is not. The difference between
collecting a creator's work and collecting their photograph of a sunset is the difference
between a copyright problem and no copyright problem at all.

Whether a particular use is lawful turns on the fair use factors — purpose, nature of the
work, amount taken, market effect — and on the equivalent doctrine in your jurisdiction.
Archiving for research, journalism and incident response is a much stronger position than
republication, and publication without permission is the position most likely to produce a
complaint.

**Do not assume a platform's Terms of Service grant you any copyright permission.** They
generally do not.

## Data protection and privacy

**Collecting public data is still processing personal data.** A public post with a name, a
face, a workplace, a location or a health condition in its caption is personal data about an
identifiable person. The fact that it was freely visible does not take it out of scope, and it
does not remove the obligations.

If you are in the EU, the UK, or anywhere with GDPR-equivalent legislation, and the subjects
are in scope:

- **You need a lawful basis.** Legitimate interests is the usual one for research, archiving
  and journalism, but it is not available in the abstract — you have to balance your interest
  against the subject's rights, and a public-interest-interest balance test may apply.
- **Special category data is different.** Data revealing race, political opinions, religious
  or philosophical beliefs, trade union membership, genetic or biometric data, or health and
  sex life, needs an Article 9 condition on top of the Article 6 basis. Scrape at your peril.
- **You may need a DPIA.** A systematic and extensive evaluation of personal aspects on a
  large scale — which a bulk collection of a large profile is — triggers an Article 35 data
  protection impact assessment.
- **You are a controller.** The manifest is your record of processing: what you collected, from
  whom, when. Keep it. It is what answers a data subject access request, and it is what shows
  you were operating within a declared purpose.
- **Data subject rights do not disappear.** Access, rectification, erasure and objection can
  all be directed at you. A collection you cannot account for is a collection you cannot
  answer for.
- **Retention is a duty, not a preference.** Art. 5(1)(e) requires storage limitation. Decide
  how long you keep a collection and automate the deletion.
- **Transfers count.** Moving a collection to another jurisdiction, or to a service outside
  it, can be a restricted transfer.

Jurisdictions outside the EU and UK have their own regimes — the CCPA and CPRA in California,
for instance, which give California residents the right to know and to delete, and which
contain their own exemptions for public data. They are not interchangeable with GDPR.

**The practical summary:** collect the narrowest thing you need, from the fewest people, for
the shortest time, with the manifest intact, and be able to explain to a person why you have
their content.

## Session cookies are credentials

With `--cookies`, the tool decrypts and reads your browser's cookie database. Those cookies
**are your live login session** for every site you are signed in to — email, banking, social,
intranet — not only for the platform being scraped.

The tool stores what it reads under `~/.cache/osint-scraper/`, and the exported file and the
cache are written `0600` with the store directory `0700`, precisely because these are
credentials. Treat them accordingly:

- **Never commit an exported `cookies.txt`.** It is a password file.
- **Never send one to anyone**, including to yourself over chat, and never paste one into an
  issue or a log.
- **Use a dedicated browser profile** for collection runs, containing only the session you
  actually need.
- **Do not run this tool on a machine with privileged access to anything else.** The tool runs
  with your privileges and reads your browser's secrets.
- **Revoke by signing out**, and by clearing the browser profile, if a machine is shared or
  compromised.

The tool never transmits cookies anywhere except to the platform being contacted, and never
writes them to the manifest. That is a property you should keep verifying.

## Acceptable use

This tool is for **research, archiving, journalism, incident response, compliance review and
personal record-keeping of content you are entitled to collect**.

It is not for:

- Collecting private, protected, follow-gated or otherwise non-public content you have no
  authorisation to access.
- Harassment, stalking, doxxing, or building a dossier on an individual.
- Surveillance of a person who has not consented to it.
- Circumventing access controls, CAPTCHAs or login walls.
- Defeating rate limits, rotating identities, or distributing traffic to look like many users.
- Republishing collected media without permission or a lawful basis.
- Bypassing a platform's technical measures to keep automated access out.
- Any purpose whose first step is "the people in this data did not agree to it".

Violating these does not just create legal risk. It is the thing that gets an operator's account
flagged, and it does real harm to the people whose content ends up in a collection.

## No warranty

**This software is provided "as is", without warranty of any kind, express or implied,
including but not limited to the warranties of merchantability, fitness for a particular
purpose and non-infringement.**

The authors and contributors make no warranty that the software will work without error, that
it will comply with any platform's terms of service, that the data it collects is accurate,
complete, lawful to collect or fit for any purpose, or that using it will not result in the
loss of an account, a contract, a position or a legal claim.

The platforms this tool talks to change without notice and without a changelog. The
transport mechanisms are reverse-engineered from observed behaviour, and Meta rotates the query
identifiers on which the Threads transport depends. A technique that works today can stop
working tomorrow, and the tool's fallbacks degrade the result rather than fail loudly.

## Limitation of liability

**In no event shall the authors, contributors or copyright holders be liable for any claim,
damages, loss, cost or expense — including reasonable legal and professional fees — arising out
of, or in connection with, the use or inability to use this software, or any collection
conducted with it, however caused and however it was caused.**

This applies whether the claim is framed in contract, tort (including negligence),
misrepresentation, statutory duty, or any other basis, and whether or not the authors were
advised of the possibility of such damages.

This applies to the software's own operation and to every collection made with it. **The
person who runs the tool is the operator.** The decisions — which targets, whether you have
authority, whether the collection is lawful, how long you keep it, who you show it to — are
yours, and they are yours alone.

## Indemnity

You agree to indemnify, defend and hold harmless the authors, contributors and copyright
holders of this software against any and all claims, demands, losses, expenses, damages,
fines, penalties, costs and liabilities of any kind, including reasonable legal and
professional fees, arising out of or related to:

- your use of, or inability to use, this software;
- any collection, processing, storage, distribution, publication or other handling of data
  obtained with this software;
- your breach of this disclaimer, of any platform's terms of service, or of any applicable
  law, regulation or court order;
- any allegation that material obtained with this software infringes the rights of a third
  party, including copyright, privacy, database or personality rights.

This indemnity survives any termination of your use of the software and is independent of the
limitation of liability above.

## Governing law

**This software is licensed under the MIT licence.** The MIT licence is the governing licence
for the software itself; see [`LICENSE`](LICENSE).

To the extent permitted by law, the disclaimer, warranty disclaimer, limitation of liability
and indemnity in this document apply to the maximum extent, and any provision that cannot be
enforced is severed without affecting the remainder.

Nothing in this document excludes or limits liability that cannot lawfully be excluded or
limited, including liability for death or personal injury caused by negligence, or for fraud
or fraudulent misrepresentation. If a provision of this document would do so, it does not
apply, and the remainder of this document continues in force.

## Third-party components

This software depends on third-party packages, including `instaloader`, `httpx`, `typer`,
`rich` and `pyyaml`, and optionally on `gallery-dl`, `yt-dlp` and `secretstorage`. Those
projects carry their own licences and are used under those terms. Their licences are not
modified by anything in this document.

The tool talks to third-party services — Instagram, Threads and their CDNs — which are subject
to their own terms of service and privacy policies. Those policies govern what the platforms
do with the data you send them, which is a different question from what you do with the data
you collect from them.

## Reporting a concern

If you believe this tool or this repository presents a legal risk that should be addressed,
opens a security hole, or has a defect that could lead to a collection going wrong, please
report it privately to the maintainers rather than opening a public issue.

A disclosure that demonstrates good faith — a clear description, a reproduction, and a
reasonable opportunity to fix it — will not be treated as actionable misconduct. Please do not
include real session cookies, real account credentials, or personal data belonging to third
parties in a report.

## Changes to this disclaimer

This disclaimer may change as the software does and as the law does. The version in the
repository is the version that applies to that revision of the software. There is no separate
"current version" — check the commit you are running.
