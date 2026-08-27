"""Build the HTML fixtures from the JSON fixtures.

The tests need a profile page that looks like the one LinkedIn serves. Rather
than commit a huge real page, we wrap our own JSON fixture in the same hidden
<code> blocks LinkedIn uses. The parser sees the same structure.

Run:  python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import pathlib

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"

PAGE = """<!DOCTYPE html>
<html lang="en"><head><title>Ada Lovelace | LinkedIn</title>
<meta name="description" content="Principal Engineer at Analytical Engines">
</head><body>
<div id="main">Server rendered markup lives here.</div>
{blocks}
<script>window.__ready = true;</script>
</body></html>
"""

BLOCK = '<code style="display: none" id="bpr-guid-{index}">{payload}</code>'

JSONLD = """<!DOCTYPE html>
<html lang="en"><head><title>Ada Lovelace | LinkedIn</title>
<script type="application/ld+json">{payload}</script>
</head><body><main>Public profile</main></body></html>
"""

PERSON = {
    "@context": "http://schema.org",
    "@graph": [
        {
            "@type": "WebPage",
            "url": "https://www.linkedin.com/in/adalovelace/",
        },
        {
            "@type": "ProfilePage",
            "mainEntity": {
                "@type": "Person",
                "name": "Ada Lovelace",
                "givenName": "Ada",
                "familyName": "Lovelace",
                "url": "https://www.linkedin.com/in/adalovelace/",
                "jobTitle": ["Principal Engineer"],
                "description": "I build systems that stay up.",
                "image": {
                    "@type": "ImageObject",
                    "contentUrl": "https://media.licdn.com/dms/image/v2/D5603AQ/photo.jpg",
                },
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Bengaluru",
                    "addressCountry": "IN",
                },
                "worksFor": [
                    {
                        "@type": "Organization",
                        "name": "Analytical Engines",
                        "url": "https://www.linkedin.com/company/analytical-engines/",
                        "member": {
                            "@type": "OrganizationRole",
                            "startDate": "2021-05",
                            "description": "Own the storage layer.",
                        },
                    }
                ],
                "alumniOf": [
                    {
                        "@type": "EducationalOrganization",
                        "name": "Indian Institute of Technology, Bombay",
                        "url": "https://www.linkedin.com/school/iit-bombay/",
                        "member": {
                            "@type": "OrganizationRole",
                            "startDate": "2013",
                            "endDate": "2017",
                        },
                    }
                ],
                "knowsLanguage": [{"@type": "Language", "name": "English"}],
            },
        },
    ],
}


def build_profile_page() -> str:
    payload = json.loads((FIXTURES / "dash_profile.json").read_text())
    included = payload["included"]

    # LinkedIn splits one profile across several blocks. Do the same, so the
    # test proves that we merge them.
    half = len(included) // 2
    chunks = [
        {"data": payload["data"], "included": included[:half]},
        {"data": {"$type": "com.linkedin.restli.common.CollectionResponse"},
         "included": included[half:]},
    ]
    blocks = "\n".join(
        BLOCK.format(index=1000 + i, payload=json.dumps(chunk, separators=(",", ":")))
        for i, chunk in enumerate(chunks)
    )
    # A decoy block that is not JSON. The parser must skip it, not crash.
    blocks += '\n<code id="bpr-guid-9999">not json at all</code>'
    return PAGE.format(blocks=blocks)


def main() -> None:
    (FIXTURES / "profile_page.html").write_text(build_profile_page())
    (FIXTURES / "public_page.html").write_text(
        JSONLD.format(payload=json.dumps(PERSON, separators=(",", ":")))
    )
    (FIXTURES / "authwall_page.html").write_text(
        "<!DOCTYPE html><html><body><div class='authwall'>"
        "Join now to view Ada's full profile</div></body></html>"
    )
    print(f"Wrote fixtures into {FIXTURES}")


if __name__ == "__main__":
    main()
