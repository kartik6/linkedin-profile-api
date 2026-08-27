# LinkedIn Profile API

A hosted API. Send a LinkedIn profile URL. Get structured JSON back.

```bash
curl "https://linkedin-profile-api.fly.dev/api/v1/profile?url=https://www.linkedin.com/in/satyanadella/"
```

**Live API:** https://linkedin-profile-api.fly.dev
**Docs:** https://linkedin-profile-api.fly.dev/docs
**Source:** https://github.com/kartik6/linkedin-profile-api

---

## Contents

1. [Approach](#approach) — how the API was found
2. [What it returns](#what-it-returns)
3. [API reference](#api-reference)
4. [Setup](#setup)
5. [Deploy](#deploy)
6. [Operations](#operations)
7. [Tests](#tests)
8. [Known limitations](#known-limitations)
9. [Legal note](#legal-note)

---

## Approach

Every route, argument and field name below was verified by hand against
LinkedIn on **28 August 2026**. Where something could not be verified, this
document says so.

### The method

LinkedIn's private API is undocumented, so there is one source of truth: a
client that already works. The browser is that client. The whole exercise is to
watch a conversation that succeeds, then reproduce it.

### 1. First, prove the account can see the data

Before debugging any code, the target profile was opened in a browser logged in
as the same account the service uses. It rendered fully.

That single check separates two problems that look identical from the outside:
a request we built wrong, and an account that is not allowed to see the data. It
cost thirty seconds and made every later result meaningful.

### 2. The profile page does not use an API

The obvious assumption is that the page fetches its data over XHR. It does not.

A DevTools search across every response body, for a string visible on the
profile, matched exactly one file: **the HTML document**. No GraphQL call
carried it.

The page is server rendered, and what it embeds is not domain data:

```html
<script id="rehydrate-data">window.__como_rehydration__ = [ "1:I[...]\n2:I[...]" ]</script>
```

That is a React Server Components flight stream — 161 strings which join into
375 chunks. Counting every `$type` inside it gives 2221 values, and **not one is
a domain entity**:

| Namespace | Count | Describes |
|---|---|---|
| `proto.sdui.actions` | 869 | what happens on click |
| `proto.sdui.expressions` | 533 | conditional logic |
| `proto.sdui.triggers` | 288 | hover, click, visibility |
| `proto.sdui.bindings` | 163 | state wiring |
| `proto.sdui.components` | 61 | text style, colour |

`SDUI` is Server Driven UI. The server sends a description of the interface, not
the data behind it. LinkedIn's server has already flattened *"this person works
at Acme"* into *"draw a text node reading Acme here"*. The app calls itself
`flagship-web`, not `voyager-web`.

**So parsing the profile page is a dead end.** There is no `Position` or
`Education` in it to read.

### 3. But Voyager is still alive underneath

The page's navigation bar still calls the old API to fetch the logged-in user's
own avatar. Replaying that call with a *different* member's ID returned that
member's URN — so the route serves any member, not just yourself.

The response was only 1334 bytes, holding two fields. That is not a
restriction; it is a **projection**. In Voyager's GraphQL the `queryId` *is* the
query — a hash identifying a stored server-side query that fixes which fields
come back. That particular one is a cache check.

It also shipped a `microSchema` describing what it had just returned:

```json
"baseType": "com.linkedin.voyager.dash.identity.profile.Profile"
```

The domain model still exists. It just needs a route that asks for the fields.

### 4. Probing for routes that do

Candidate routes were fired in batches and read by status code. The failures
were as informative as the successes:

| Route | Status | Meaning |
|---|---|---|
| `/identity/profiles/{id}/profileView` | **410 Gone** | Deliberately retired. 410, not 404 — LinkedIn is saying "this existed, stop asking". |
| `/identity/dash/profileCards/...` | 404 | No such route. Card URNs are for the SDUI layer only. |
| `/identity/dash/profiles?q=publicIdentifier` | 400 | Route exists, query name wrong. |
| `/identity/dash/profiles?q=memberIdentity` **with a full URN** | 403 | Route exists, argument format wrong. |
| `/identity/dash/profiles?q=memberIdentity` **with a bare id or vanity** | **200** | Works. |
| `/identity/dash/profilePositions?q=viewee&profileUrn=` | **200** | Works. |

Once `profilePositions`, `profileEducations` and `profileSkills` worked, the
pattern was clear enough to predict from:

```
/voyager/api/identity/dash/profile{Entity}s?q=viewee&profileUrn={urn}
```

Eleven more section names were predicted from that shape. **Ten of eleven
existed.** `viewee` is LinkedIn's own word for "the person being viewed".

### 5. Reading responses by size

Eight sections returned exactly 232 bytes. That is not an error — it is the
empty-collection baseline. A valid 200 with zero elements, because the test
subject genuinely has no patents or publications.

| Bytes | Meaning |
|---|---|
| 14 | `{"status":404}` — no such route |
| 232 | works; the person has nothing in this section |
| 2000+ | real data |

Knowing that constant lets you read a whole probe sweep without opening a body.

### 6. The verified chain

```
profile URL
  → vanity name                                    urls.py
  → GET /identity/dash/profiles
        ?q=memberIdentity&memberIdentity={vanity}   one call, returns the top card
  → read entityUrn from the Profile in `included`
  → GET /identity/dash/profile{Section}s
        ?q=viewee&profileUrn={urn}                  one call per section
  → merge every `included` array into one pool
  → normalize into our schema
```

Authentication is two cookies plus a header. `li_at` is the session. The
`csrf-token` header must carry the `JSESSIONID` cookie value with its quotes
stripped — `evil.com` can make your browser send cookies but cannot read them,
so copying the value into a header proves the request came from real LinkedIn
JavaScript.

### 7. The bug that only production revealed

The first deployment failed on every request. Our own error code said
`profile_not_found`, which was wrong and self-inflicted.

The real response was `302`, redirecting to **the same URL it had asked for**.
That is LinkedIn's routing-cookie handshake: it answers with `Set-Cookie:
lidc=...` and expects a retry. A browser does that automatically. Our client
sent a fixed cookie dictionary on every request and followed no redirects, so it
never stored `lidc` and was bounced forever.

Each session now owns its own `httpx` client, and therefore its own cookie jar.
`tests/test_strategies.py::TestRoutingCookieHandshake` encodes the failure, and
`scripts/e2e.py --mode handshake` reproduces it end to end.

### 8. What the responses do not contain

Counting fields across *all* eleven captured positions, not one sample:

```
11/11  title           10/11  companyName      5/11  locationName
11/11  dateRange        9/11  companyUrn       0/11  description
```

`description` appears on **zero**. `profilePositions` does not return role
descriptions at all. That is a capability limit, documented below, not a bug.

Sampling one position would have been misleading in the other direction too:
the first one examined happened to be the one *without* `companyName`, which
almost led to a wrong conclusion about where company data lives.

### 9. Why only one strategy

An earlier version of this service had four fetch strategies. Three were removed
after testing proved them dead — 410 Gone, or looking for a payload format the
page no longer ships. They are documented in
`app/linkedin/strategies/__init__.py` with the evidence for each removal.

One strategy that works is worth more than four that might.

---

## What it returns

```jsonc
{
  "profile": {
    "public_identifier": "some-person-123456",
    "urn": "urn:li:fsd_profile:ACoAA...",
    "first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace",
    "headline": "Principal Engineer | Distributed Systems",
    "about": "I build systems that stay up.",
    "pronouns": "he/him",
    "location": { "country_code": "IN", "full": null },
    "profile_picture": {
      "url": "https://media.licdn.com/dms/image/v2/.../800_800/...",
      "artifacts": [
        { "width": 100, "height": 100, "url": "..." },
        { "width": 800, "height": 800, "url": "..." }
      ],
      "expires_at": "2026-09-17T00:00:00Z"
    },
    "background_picture": { "url": "..." },

    "experience": [{
      "title": "Principal Engineer",
      "company": { "name": "Acme", "urn": "urn:li:fsd_company:9001",
                   "linkedin_url": "https://www.linkedin.com/company/9001/" },
      "location": "Bengaluru, Karnataka, India",
      "description": null,
      "date_range": {
        "start": { "year": 2023, "month": 2, "text": "February 2023" },
        "end": null, "is_current": true,
        "duration_months": 43, "text": "February 2023 - Present"
      }
    }],
    "education":      [{ "school": {...}, "degree": "...", "field_of_study": "...", "grade": "..." }],
    "skills":         [{ "name": "Rust" }],
    "certifications": [{ "name": "...", "authority": "...", "license_number": "...", "issued_on": {...} }],
    "languages": [], "projects": [], "honors": [], "volunteering": [],
    "publications": [], "courses": [], "patents": [], "organizations": [], "test_scores": []
  },

  "meta": {
    "strategy": "voyager_dash",
    "cached": false,
    "fetched_at": "2026-08-28T10:30:00Z",
    "duration_ms": 3120,
    "completeness": 1.0,
    "partial": false,
    "warnings": []
  }
}
```

Every field is optional. An empty section usually means the person has nothing
there — LinkedIn returns a valid empty collection, not an error. Read
`meta.warnings` to tell that apart from a section call that failed.

`completeness` scores coverage against `experience`, `education` and `skills`
plus the core scalar fields. Languages, patents and the rest are deliberately
not scored, because most real profiles have none.

---

## API reference

Base URL: `https://linkedin-profile-api.fly.dev`

### `GET /api/v1/profile`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | A profile URL, or a bare public identifier |
| `refresh` | bool | no | Skip the cache and refetch |

These all resolve to the same profile:

```
https://www.linkedin.com/in/satyanadella/
https://in.linkedin.com/in/satyanadella?trk=public_profile
linkedin.com/in/satyanadella
satyanadella
urn:li:fsd_profile:ACoAAA1234
```

### `POST /api/v1/profile`

```bash
curl -X POST "$BASE/api/v1/profile" -H 'content-type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/satyanadella/", "refresh": false}'
```

### `POST /api/v1/profiles/batch`

Up to 10 profiles, 3 at a time. One failure does not sink the batch.

### Operations endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness. No auth. |
| `GET /api/v1/session` | Is the LinkedIn cookie still valid |
| `GET /api/v1/diagnose?url=` | Raw status, landing URL and body head for each route |
| `GET /api/v1/strategies` | List the fetch strategies |
| `GET /api/v1/parse?url=` | Validate a URL, no LinkedIn call |
| `GET /api/v1/cache` | Hit rate and entry count |
| `DELETE /api/v1/cache/{id}` | Drop one cached profile |
| `GET /docs` | Interactive OpenAPI reference |

`/api/v1/diagnose` exists because our own error codes say what *we* did about a
failure, not what LinkedIn said. It reports the raw upstream answer. It is what
found the `302` handshake.

### Authentication

Set `API_KEYS` and every call needs `X-API-Key`. Leave it empty and the API is
open, which suits a demo.

### Errors

```json
{ "error": "linkedin_session_invalid",
  "message": "The LinkedIn session is no longer valid. Refresh the cookies.",
  "detail": null }
```

| HTTP | `error` | What to do |
|---|---|---|
| 400 | `invalid_profile_url` | Check the URL |
| 401 | `unauthorized` | Send `X-API-Key` |
| 404 | `profile_not_found` | Check the identifier |
| 429 | `rate_limited` | Wait for `Retry-After` |
| 429 | `linkedin_rate_limited` | Wait. Lower `OUTBOUND_RPS` |
| 502 | `all_strategies_failed` | Check `/api/v1/diagnose` |
| 503 | `linkedin_session_invalid` | Replace `LI_AT` |
| 503 | `linkedin_challenge_required` | Log in through a browser and clear the check |
| 503 | `no_linkedin_session` | Set `LI_AT` |

When every strategy fails the same way, that specific error is returned rather
than a generic one. A dead cookie and a bot check need different responses, and
`all_strategies_failed` would tell an operator neither.

A partial profile is **not** an error. It returns 200 with `meta.partial: true`.

---

## Setup

Requires Python 3.11+ and a LinkedIn account. **Use a throwaway account.**

```bash
git clone https://github.com/kartik6/linkedin-profile-api.git
cd linkedin-profile-api
make install
cp .env.example .env      # then add your cookies
make run                  # http://127.0.0.1:8080/docs
```

### Getting the cookies

1. Log in to LinkedIn in a browser with the throwaway account.
2. DevTools → **Application** → **Cookies** → `https://www.linkedin.com`.
3. Copy `li_at` and `JSESSIONID` into `.env`.

`li_at` is `HttpOnly`, so `document.cookie` will not show it. Use the
Application tab, or read the `Cookie:` request header in the Network tab.

```bash
curl localhost:8080/api/v1/session
# {"configured": true, "authenticated": true, "logged_in_as": "your-handle"}
```

The cookie dies if you log out, so close the tab instead.

---

## Deploy

```bash
fly auth login
fly apps create linkedin-profile-api
fly deploy --remote-only --ha=false
fly secrets set LI_AT='AQEDAT...' JSESSIONID='ajax:1234567890123456789'
```

**Run one machine.** Fly creates two by default. The outbound rate limiter and
the cache both live in process memory, so a second machine doubles the request
rate toward LinkedIn and halves the cache hit rate. Request rate is the main
signal LinkedIn uses to decide to challenge a session.

Set `primary_region` in `fly.toml` near the account's country. Fly retires
regions — `bom` no longer accepts machines. Run `fly platform regions` if a
deploy is rejected.

---

## Operations

```bash
curl "$BASE/api/v1/session"                 # is the cookie alive
curl "$BASE/api/v1/diagnose?url=<profile>"  # what LinkedIn actually answered
fly secrets set LI_AT='...' JSESSIONID='...'  # rotate; restarts automatically
fly secrets set LI_AT_POOL='cookieA:ajax:111,cookieB:ajax:222'  # several accounts
```

| Variable | Default | Notes |
|---|---|---|
| `OUTBOUND_RPS` | `1.0` | Calls per second toward LinkedIn, all callers combined |
| `SECTIONS` | all | Comma separated. Fewer sections means fewer calls per profile |
| `CACHE_TTL_S` | `3600` | Longer means fewer LinkedIn calls |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per caller limit on our own API |
| `REDIS_URL` | unset | Share the cache across instances |

A full profile costs **1 + 13 calls**. At `OUTBOUND_RPS=1.0` that is about 14
seconds. Trim `SECTIONS` to the ones you need if that is too slow.

---

## Tests

```bash
make test     # 95 tests, no credentials needed
make lint
make e2e      # the whole stack against a mock LinkedIn, 5 failure modes
```

```
ok   all         strategy=voyager_dash  complete=1.0    experience=11 skills=20 certs=12
ok   handshake   strategy=voyager_dash  complete=1.0    experience=11 skills=20 certs=12
ok   thin        strategy=voyager_dash  complete=0.667  experience=0  skills=0  certs=0
ok   dead        http=503 error=linkedin_session_invalid
ok   challenge   http=503 error=linkedin_challenge_required
```

**Fixtures come from real captured responses**, scrubbed of personal data by
`scripts/make_fixtures.py`. The structure is untouched: real field names, real
nesting, real types.

This matters. An earlier version of this suite used hand-written fixtures that
matched what the author *believed* LinkedIn returns. Ninety-six tests passed and
none could have caught that the belief was wrong. A test built on an assumption
cannot test that assumption.

To check the parsers against production:

```bash
python scripts/capture.py https://www.linkedin.com/in/<name>/
```

### Layout

```
app/
  main.py          HTTP routes, error handlers, OpenAPI
  models.py        the response schema — the contract
  config.py        settings; every secret comes from the environment
  cache.py         memory cache, optional Redis
  errors.py        error types, each with a stable code
  linkedin/
    urls.py        any URL shape -> public identifier
    session.py     cookie jar per session, Voyager headers, redirect watcher
    client.py      HTTP, pacing, retry, failure naming
    entities.py    index the flat entity graph
    text.py        read LinkedIn's four text wrappers
    images.py      VectorImage -> real URLs
    dates.py       partial dates and durations
    normalize.py   raw payloads -> Profile
    service.py     orchestration, merge, score
    strategies/    the verified strategy, and why the others were removed
scripts/
  mock_linkedin.py  serves the fixtures, reproduces five failure modes
  e2e.py            full stack check
  capture.py        record real payloads for debugging
  make_fixtures.py  rebuild fixtures from captures, scrubbing personal data
```

---

## Known limitations

**Legal.** Automated access breaks LinkedIn's User Agreement. See below.

**Role descriptions are not available.** Verified: `description` appeared on 0
of 11 real positions. `profilePositions` does not return it. The field stays in
the schema and is always `null`.

**Location is only a country code.** The API returns
`location: {countryCode: "IN"}` and `geoLocation: {geoUrn: ...}`. The display
name — "Greater Bengaluru Area" — is not in the response. Resolving `geoUrn`
needs a route we have not mapped.

**Company and industry are URNs, not names.** `companyUrn` and `industryUrn` come
back unresolved, and `included` is empty on these routes, so there are no logos
or industry names without extra calls.

**Employment type is a URN.** `employmentTypeUrn: urn:li:fsd_employmentType:20`
with no lookup table, so `employment_type` is always `null`.

**Follower and connection counts are absent** from the routes we verified.

**14 calls per profile.** One top card plus 13 sections. That is slow and it is
the main risk to the account. Trim `SECTIONS`.

**One machine only.** The rate limiter and cache are per process. Two instances
double the real outbound rate. Redis shares the cache but not the limiter.

**The account can get restricted.** Use a throwaway. Keep `OUTBOUND_RPS` low.
The API returns `linkedin_challenge_required` rather than hiding it.

**Datacenter IPs draw more checks** than residential ones. No proxy support.

**Visibility follows the login.** The same profile through two different cookies
returns different data. A stranger sees less than a 1st-degree connection.

**Image URLs expire.** They are signed and last hours. `expires_at` says when.

**Verified against one profile.** Findings come from a single real profile
captured on 28 August 2026. Field presence may vary. `scripts/capture.py` is
how you check another.

### With more time

1. Resolve `geoUrn`, `companyUrn`, `industryUrn` into names.
2. A shared rate limiter in Redis, so many instances stay under one budget.
3. Parse the SDUI stream as a fallback for when the REST routes are retired.
4. A scheduled job that captures a real profile and fails CI when a shape
   changes — so the parser breaks in CI, not in production.

---

## Legal note

This project reads LinkedIn data through a logged-in session. That breaks
LinkedIn's User Agreement, which forbids automated access. LinkedIn may restrict
or close an account it detects.

*hiQ Labs v. LinkedIn* found that scraping **public** data is not a Computer
Fraud and Abuse Act violation. That ruling does not cover data behind a login
and does not override a contract. This service reads data behind a login.

Profile data is personal data under the GDPR and the DPDP Act. A lawful basis is
needed to store it.

Built for a hiring challenge, to show how the API works. LinkedIn's official
[Marketing and Talent Solutions APIs](https://learn.microsoft.com/en-us/linkedin/)
are the supported path.

---

## License

MIT. See [LICENSE](LICENSE).
