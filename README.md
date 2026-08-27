# LinkedIn Profile API

A hosted API. Send a LinkedIn profile URL. Get structured JSON back.

```bash
curl "https://linkedin-profile-api.fly.dev/api/v1/profile?url=https://www.linkedin.com/in/satyanadella/"
```

**Live API:** `https://linkedin-profile-api.fly.dev` — *(replace after you deploy)*
**Docs:** [`/docs`](https://linkedin-profile-api.fly.dev/docs) — interactive OpenAPI reference.

---

## Contents

1. [What it returns](#what-it-returns)
2. [Approach](#approach)
3. [API reference](#api-reference)
4. [Setup](#setup)
5. [Deploy](#deploy)
6. [Operations](#operations)
7. [Tests](#tests)
8. [Known limitations](#known-limitations)
9. [Legal note](#legal-note)

---

## What it returns

The response holds two objects. `profile` holds the data. `meta` tells you where
the data came from and how complete it is.

```jsonc
{
  "profile": {
    "public_identifier": "adalovelace",
    "urn": "urn:li:fsd_profile:ACoAAAB1234",
    "profile_url": "https://www.linkedin.com/in/adalovelace/",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "full_name": "Ada Lovelace",
    "headline": "Principal Engineer at Analytical Engines",
    "about": "I build systems that stay up.",
    "industry": "Software Development",
    "location": {
      "full": "Bengaluru, Karnataka, India",
      "city": "Bengaluru",
      "country": "India",
      "country_code": "IN"
    },
    "profile_picture": {
      "url": "https://media.licdn.com/.../800_800/0/16?e=1767225600&v=beta&t=cc",
      "artifacts": [
        { "width": 100, "height": 100, "url": "https://media.licdn.com/..." },
        { "width": 400, "height": 400, "url": "https://media.licdn.com/..." },
        { "width": 800, "height": 800, "url": "https://media.licdn.com/..." }
      ],
      "expires_at": "2026-01-01T00:00:00Z"
    },
    "background_picture": { "url": "..." },
    "follower_count": 18422,
    "connection_count": 500,
    "open_to_work": false,
    "is_premium": true,

    "experience": [
      {
        "title": "Principal Engineer",
        "company": {
          "name": "Analytical Engines",
          "urn": "urn:li:fsd_company:9001",
          "linkedin_url": "https://www.linkedin.com/company/analytical-engines/",
          "logo": { "url": "https://media.licdn.com/..." },
          "industry": "Software Development",
          "staff_count": 2400
        },
        "employment_type": "FULL_TIME",
        "workplace_type": "HYBRID",
        "location": "Bengaluru, India",
        "description": "Own the storage layer.",
        "date_range": {
          "start": { "year": 2021, "month": 5, "text": "May 2021" },
          "end": null,
          "is_current": true,
          "duration_months": 64,
          "text": "May 2021 - Present"
        },
        "skills": ["Rust", "Distributed Systems"]
      }
    ],
    "education":      [{ "school": {...}, "degree": "...", "field_of_study": "...", "grade": "...", "date_range": {...} }],
    "skills":         [{ "name": "Rust", "endorsement_count": 17 }],
    "certifications": [{ "name": "...", "authority": "...", "license_number": "...", "url": "...", "issued_on": {...}, "expires_on": {...} }],
    "languages":      [{ "name": "English", "proficiency": "Native Or Bilingual" }],
    "projects":       [], "publications": [], "honors": [], "volunteering": [],
    "courses":        [], "patents": [], "organizations": [], "test_scores": []
  },

  "meta": {
    "strategy": "voyager_profile_view",
    "strategies_tried": ["voyager_profile_view"],
    "cached": false,
    "fetched_at": "2026-08-27T10:30:00Z",
    "duration_ms": 842,
    "completeness": 1.0,
    "partial": false,
    "warnings": []
  }
}
```

Every field is optional. LinkedIn shows different data to different viewers, so
a field that is absent is normal, not an error. Read `meta.completeness` and
`meta.warnings` to see what is missing and why.

---

## Approach

### 1. Find the real API

LinkedIn's web app is a single page app. It does not render the profile on the
server for the browser to read. It calls a private JSON API and renders the
result. That API is **Voyager**, at `www.linkedin.com/voyager/api/`.

I opened a profile with the browser network panel filtered to XHR, and read the
requests the page made. Three facts came out of that:

**Authentication is two cookies, not a token.**

| Item | Value | Purpose |
|---|---|---|
| `li_at` cookie | opaque session string | identifies the member |
| `JSESSIONID` cookie | `"ajax:1234567890123456789"` | CSRF pair, quoted |
| `csrf-token` header | `ajax:1234567890123456789` | same value, quotes removed |

The header and the cookie must match. A request with one and not the other gets
a `401`. Voyager also wants `x-restli-protocol-version: 2.0.0`, because it
speaks [Rest.li](https://linkedin.github.io/rest.li/), LinkedIn's own REST
dialect.

**One header changes the response shape.** Send
`Accept: application/vnd.linkedin.normalized+json+2.1` and Voyager answers with
a flat entity graph instead of a deep tree:

```jsonc
{
  "data":     { "*elements": ["urn:li:fsd_profile:ACoAA..."] },
  "included": [
    { "$type": "...profile.Profile",  "entityUrn": "urn:li:fsd_profile:ACoAA...", "firstName": "Ada" },
    { "$type": "...profile.Position", "entityUrn": "urn:...", "title": "Engineer", "*company": "urn:li:fsd_company:9001" },
    { "$type": "...organization.Company", "entityUrn": "urn:li:fsd_company:9001", "name": "Acme" }
  ]
}
```

Each entity carries a type and an ID. A field whose name starts with `*` holds
a URN that points at another entity in the same list. This is a normalized
graph, so I index it once and resolve references by lookup.

**The same graph appears in three places.** The REST route returns it. The
GraphQL route returns it. And the server rendered HTML page **inlines it** in
hidden elements:

```html
<code style="display:none" id="bpr-guid-3921884">{"data":{...},"included":[...]}</code>
```

That last one matters more than it looks. It means I can read a full profile
from the HTML page with no knowledge of any API route at all.

### 2. Build four ways in, not one

A single scraper against a private API is one deploy away from dead. So the
service holds four strategies and walks them in order. The first that returns
enough data wins.

| # | Strategy | Route | Cost | Breaks when |
|---|---|---|---|---|
| 1 | `voyager_profile_view` | `GET /identity/profiles/{id}/profileView` | 1 call | LinkedIn retires the legacy route for the account |
| 2 | `voyager_dash` | `GET /identity/dash/profiles` + GraphQL per section | 1 to 11 calls | a decoration ID or a GraphQL query hash rotates |
| 3 | `embedded_json` | `GET /in/{id}/` then parse inlined JSON | 1 call, larger | LinkedIn stops server rendering the page |
| 4 | `public_jsonld` | `GET /in/{id}/` with **no cookie**, parse schema.org | 1 call | the profile is not public |

Strategy 1 is first because one call returns every section. Strategy 3 is the
durable one: it depends on no route name, no decoration ID and no query hash,
only on the public profile URL. Strategy 4 needs no login at all, so the API
still answers something when every cookie is dead.

**The important design choice:** all four feed **one** normalizer. Strategies 2,
3 and 4 differ only in how they obtain the entity graph, not in how they read
it. Adding a fifth way in means writing a fetch, not a parser.

```
URL ─→ parse ─→ [ strategy 1 ] ─┐
                [ strategy 2 ] ─┤
                [ strategy 3 ] ─┼─→ EntityPool ─→ normalize ─→ Profile ─→ JSON
                [ strategy 4 ] ─┘
```

### 3. Survive LinkedIn's renaming

LinkedIn ships two namespaces at the same time for the same idea:

```
com.linkedin.voyager.identity.profile.Position          # older
com.linkedin.voyager.dash.identity.profile.Position     # newer
```

So the index matches on the **last segment** of the type, not the full name.
`Position` matches both. This one decision made the parser work against payload
shapes I had not seen when I wrote it.

Every field read tries several key names, because LinkedIn moves fields between
releases. Text arrives in at least four wrappers, so one helper reads all of
them:

```python
"Engineer"                                # plain
{"text": "Engineer"}                      # TextViewModel
{"text": {"text": "Engineer"}}            # nested
{"en_US": "Engineer", "de_DE": "..."}     # locale map
```

### 4. Rebuild the images

LinkedIn never sends an image URL. It sends a root and one path segment per
size:

```jsonc
{
  "rootUrl": "https://media.licdn.com/dms/image/v2/D5603AQ.../profile-displayphoto-shrink_",
  "artifacts": [
    { "width": 100, "height": 100, "fileIdentifyingUrlPathSegment": "100_100/0/16?e=1767225600&v=beta&t=aa" },
    { "width": 800, "height": 800, "fileIdentifyingUrlPathSegment": "800_800/0/16?e=1767225600&v=beta&t=cc" }
  ]
}
```

A usable URL is `rootUrl + fileIdentifyingUrlPathSegment`. The API returns every
size, and picks the largest as the default. The `e=` parameter is a unix
expiry, so the API also returns `expires_at`. **These URLs stop working after a
few hours.** Download the bytes if you need them to last.

### 5. Never fail on a section

A profile has fifteen sections. One of them changing shape must not cost the
other fourteen. Each section parses inside its own guard, and a failure yields
an empty list plus a warning. The response then reports the damage:

```json
"meta": {
  "completeness": 0.75,
  "partial": true,
  "warnings": ["These sections came back empty: skills, certifications."]
}
```

A partial profile with a warning serves the caller better than a `500`.

### 6. Protect the account

The LinkedIn account is the scarce resource here, not CPU. So:

- **One shared token bucket** paces every outbound call, at `0.4` per second by
  default. Concurrent callers queue behind it.
- **Jitter** breaks up an even request rhythm.
- **A cache** with a one hour time to live. A cache hit costs LinkedIn nothing.
- **A session pool.** Give the service several cookies and it rotates them. A
  cookie that fails goes into quarantine for fifteen minutes, and the rest
  carry on.
- **Named failures.** A dead cookie returns `linkedin_session_invalid`. A bot
  check returns `linkedin_challenge_required`. An operator then knows whether
  to replace a cookie or to wait.

---

## API reference

Base URL: `https://linkedin-profile-api.fly.dev`

### `GET /api/v1/profile`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | A profile URL, or a bare public identifier. |
| `refresh` | bool | no | Skip the cache and refetch. Default `false`. |

These inputs all resolve to the same profile:

```
https://www.linkedin.com/in/satyanadella/
https://in.linkedin.com/in/satyanadella?trk=public_profile
linkedin.com/in/satyanadella
satyanadella
urn:li:fsd_profile:ACoAAA1234
```

```bash
curl "$BASE/api/v1/profile?url=https://www.linkedin.com/in/satyanadella/"
```

### `POST /api/v1/profile`

```bash
curl -X POST "$BASE/api/v1/profile" \
  -H 'content-type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/satyanadella/", "refresh": false}'
```

### `POST /api/v1/profiles/batch`

Reads up to 10 profiles, 3 at a time. One failure does not sink the batch.

```bash
curl -X POST "$BASE/api/v1/profiles/batch" \
  -H 'content-type: application/json' \
  -d '{"urls": ["satyanadella", "williamhgates"]}'
```

```json
{ "results": [ { "url": "satyanadella", "ok": true, "profile": {...}, "meta": {...} } ],
  "requested": 2, "succeeded": 2, "failed": 0 }
```

### Operations endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness. No authentication. |
| `GET /api/v1/session` | Ask LinkedIn if our cookie still works. |
| `GET /api/v1/strategies` | List the strategies and their order. |
| `GET /api/v1/parse?url=` | Validate a URL. Makes no LinkedIn call. |
| `GET /api/v1/cache` | Hit rate and entry count. |
| `DELETE /api/v1/cache/{id}` | Drop one cached profile. |
| `GET /docs` | Interactive OpenAPI reference. |

### Authentication

Set `API_KEYS` and every call needs a header:

```bash
curl -H "X-API-Key: your-key" "$BASE/api/v1/profile?url=satyanadella"
```

Leave `API_KEYS` empty and the API stays open. That suits a demo.

### Errors

Every error returns the same body.

```json
{ "error": "linkedin_session_invalid",
  "message": "The LinkedIn session is no longer valid. Refresh the cookies.",
  "detail": null }
```

| HTTP | `error` | Meaning | What to do |
|---|---|---|---|
| 400 | `invalid_profile_url` | Not a member profile URL. | Check the URL. |
| 401 | `unauthorized` | Missing or wrong API key. | Send `X-API-Key`. |
| 404 | `profile_not_found` | No profile at that identifier. | Check the identifier. |
| 429 | `rate_limited` | Our own caller limit. | Wait for `Retry-After`. |
| 429 | `linkedin_rate_limited` | LinkedIn throttled us. | Wait. Lower `OUTBOUND_RPS`. |
| 502 | `all_strategies_failed` | Every strategy failed. `detail` names each reason. | Check `/api/v1/session`. |
| 503 | `linkedin_session_invalid` | The cookie is dead. | Replace `LI_AT`. |
| 503 | `linkedin_challenge_required` | LinkedIn wants a human. | Log in through a browser and clear the check. |
| 503 | `no_linkedin_session` | No cookie is set. | Set `LI_AT`. |

A partial profile is **not** an error. It returns `200` with `meta.partial` set
to `true`.

---

## Setup

### Requirements

- Python 3.11 or later
- A LinkedIn account. **Use a throwaway account, not your main one.**

### Local

```bash
git clone https://github.com/<you>/linkedin-profile-api.git
cd linkedin-profile-api

make install            # or: python -m venv .venv && pip install -r requirements-dev.txt
cp .env.example .env    # then add your cookies
make run                # http://127.0.0.1:8080/docs
```

### Get the cookies

1. Log in to LinkedIn in a browser, with the throwaway account.
2. Open developer tools. Go to **Application**, then **Cookies**, then
   `https://www.linkedin.com`.
3. Copy the value of `li_at`.
4. Copy the value of `JSESSIONID`. It looks like `"ajax:1234567890123456789"`.
5. Put both in `.env`:

```bash
LI_AT=AQEDAT...
JSESSIONID="ajax:1234567890123456789"
```

Confirm the session works:

```bash
curl localhost:8080/api/v1/session
# {"configured": true, "authenticated": true, "logged_in_as": "your-handle", ...}
```

A cookie lasts weeks. It dies when you log out, so **do not log out** of that
browser session, and do not click "sign out of all devices".

### Docker

```bash
docker build -t linkedin-profile-api .
docker run --rm -p 8080:8080 --env-file .env linkedin-profile-api
```

---

## Deploy

The target is [Fly.io](https://fly.io). Fly gives one machine a dedicated IPv4
address in a region you choose. LinkedIn challenges shared datacenter addresses
much more often, so a stable address near the account's country raises the
success rate.

```bash
fly auth login
fly launch --no-deploy          # keeps the fly.toml in this repo

# Secrets live in Fly, never in git.
fly secrets set \
  LI_AT='AQEDAT...' \
  JSESSIONID='ajax:1234567890123456789' \
  API_KEYS='a-key-you-choose'

fly deploy
fly open /docs
```

Fly terminates TLS, so HTTPS works with no extra work. `force_https = true` in
`fly.toml` redirects plain HTTP.

Edit `primary_region` in `fly.toml` before the first deploy. Put it in the same
country as the LinkedIn account. `bom` is Mumbai. `iad` is Virginia. `lhr` is
London.

The same image runs on Render, Railway or Cloud Run. Only the secret syntax
changes.

---

## Operations

### Is the cookie still alive

```bash
curl -H "X-API-Key: $KEY" "$BASE/api/v1/session"
```

It reports each session in the pool, its failure count and its quarantine
timer. Use it to tell a dead cookie apart from a broken parser.

### Rotate a cookie

```bash
fly secrets set LI_AT='the-new-value' JSESSIONID='ajax:...'
```

Fly restarts the machine. Use `LI_AT_POOL` for several accounts:

```bash
fly secrets set LI_AT_POOL='cookieA:ajax:111,cookieB:ajax:222'
```

### Repair the GraphQL strategy without a deploy

LinkedIn rotates its GraphQL query hashes on every web release. When strategy 2
starts to fail:

1. Open a profile in a browser with the network panel open.
2. Find a request to `/voyager/api/graphql`.
3. Copy the `queryId` parameter.
4. `fly secrets set QUERY_ID_PROFILE_COMPONENTS='voyagerIdentityDashProfileComponents.<hash>'`

No code change. No new image. Strategy 3 covers the gap while you do it.

### Turn a strategy off

```bash
fly secrets set STRATEGIES='embedded_json,public_jsonld'
```

### Tuning

| Variable | Default | Notes |
|---|---|---|
| `OUTBOUND_RPS` | `0.5` | Calls per second toward LinkedIn, across all callers. Lower it if you see challenges. |
| `OUTBOUND_JITTER_MS` | `400` | Random delay added per call. |
| `CACHE_TTL_S` | `3600` | Longer means fewer LinkedIn calls. |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per caller limit on our own API. |
| `REDIS_URL` | unset | Set it to share the cache across instances. |

---

## Tests

The suite needs **no LinkedIn credentials**. It runs against recorded fixtures,
so CI is green on a fresh clone.

```bash
make test     # 90 tests
make lint
make e2e      # the whole stack against a mock LinkedIn
```

`make e2e` starts the real ASGI app, the real HTTP client and the real
strategies. Only LinkedIn is replaced. It then forces four failure modes and
checks that the right strategy takes over each time:

```
  ok   all            strategy=voyager_profile_view   complete=1.0   experience=2 skills=4
  ok   no-legacy      strategy=voyager_dash           complete=1.0   experience=2 skills=2
  ok   voyager-down   strategy=embedded_json          complete=1.0   experience=2 skills=2
  ok   logged-out     strategy=public_jsonld          complete=0.75  experience=1 skills=0
```

To debug against the real LinkedIn:

```bash
python scripts/capture.py https://www.linkedin.com/in/<name>/ --raw
```

It runs every strategy, prints what each one found, and saves the payloads to
`captures/`. That directory is in `.gitignore`, because a real capture holds
another person's personal data.

### Layout

```
app/
  main.py              HTTP routes, error handlers, OpenAPI
  models.py            the response schema. The contract.
  config.py            settings. Every secret comes from the environment.
  cache.py             memory cache, optional Redis
  deps.py              API key check, caller rate limit
  errors.py            error types, each with a stable code
  linkedin/
    urls.py            any URL shape -> public identifier
    session.py         cookie pool, Voyager headers, quarantine
    client.py          HTTP, pacing, retry, failure detection
    entities.py        index the flat entity graph
    text.py            read LinkedIn's four text wrappers
    images.py          VectorImage -> real URLs
    dates.py           partial dates and durations
    components.py      the rendered card tree fallback
    normalize.py       raw payloads -> Profile
    service.py         run the strategies, merge, score
    strategies/        the four ways in
tests/                 90 tests, fixtures, no credentials needed
scripts/
  mock_linkedin.py     a stand in for LinkedIn
  e2e.py               full stack check across four failure modes
  capture.py           record real payloads for debugging
  make_fixtures.py     rebuild the HTML fixtures
```

---

## Known limitations

**Legal.** Automated access breaks LinkedIn's User Agreement. See the
[legal note](#legal-note).

**The account can get restricted.** Volume raises the risk. LinkedIn may show a
CAPTCHA, force a password reset, or restrict the account. Use a throwaway
account. Keep `OUTBOUND_RPS` low. The API returns
`linkedin_challenge_required` when this happens, and it does not hide it.

**Datacenter IP addresses draw more checks.** A cloud address is challenged more
often than a home address. A dedicated Fly IPv4 in the account's country helps.
A residential proxy would help more. This build has no proxy support.

**Visibility follows the login, not the URL.** LinkedIn shows a 1st degree
connection more than a stranger. The same profile through two different cookies
returns different data. Contact details, full connection counts and some
sections are absent for distant viewers.

**Image URLs expire.** They are signed and last a few hours. `expires_at` says
when. Download the bytes if you need them longer.

**Some fields need extra calls that this build skips.** Recommendations, full
endorsement lists, contact details and post activity each need their own
endpoint. The schema has no field for them yet.

**GraphQL query hashes rotate.** Strategy 2 is the fragile one. It is
configurable for that reason, and strategies 3 and 4 cover the gap.

**Rate limits are per process.** The token bucket and the caller limiter live in
memory. Run more than one machine and the real outbound rate multiplies. Redis
holds the cache across instances but not the limiter. A shared limiter is the
next piece of work.

**No JavaScript is executed.** Sections that load only after a click, such as
"show all 42 skills", may come back short. The typed entity routes usually
carry the full list, and the card tree fallback does not.

**Fixtures are synthetic.** They match the payload shapes I observed, and they
are hand written so the repository holds nobody's personal data. Run
`scripts/capture.py` against a real profile to check the parsers against
production.

**Docker build is unverified in this environment.** The Dockerfile is standard
and the same command line runs in CI, but no local daemon was available to build
the image here.

### With more time

1. A shared rate limiter in Redis, so many instances stay under one budget.
2. A residential proxy pool, chosen per session.
3. A weekly job that captures real payloads and fails CI when a shape changes,
   so the parser breaks in CI and not in production.
4. Recommendations, contact details and activity.
5. A webhook mode for large batches, instead of a synchronous call.

---

## Legal note

This project reads LinkedIn data through a logged in session. That breaks
LinkedIn's User Agreement, which forbids automated access. LinkedIn may restrict
or close an account it detects.

*hiQ Labs v. LinkedIn* found that scraping **public** data is not a Computer
Fraud and Abuse Act violation. That ruling does not cover data behind a login,
and it does not override a contract. This service reads data behind a login when
a cookie is set.

Profile data is personal data under the GDPR and the DPDP Act. A lawful basis is
needed to store it, and the person has rights over it.

I built this for a hiring challenge, to show how the API works. Do not run it
against a real account at volume, and do not build a product on it without
counsel. LinkedIn's official
[Marketing and Talent Solutions APIs](https://learn.microsoft.com/en-us/linkedin/)
are the supported path.

---

## License

MIT. See [LICENSE](LICENSE).
