import pytest

from app.errors import InvalidProfileURL
from app.linkedin.urls import parse_profile_url

VALID = [
    ("https://www.linkedin.com/in/satyanadella/", "satyanadella"),
    ("https://www.linkedin.com/in/satyanadella", "satyanadella"),
    ("http://linkedin.com/in/satyanadella", "satyanadella"),
    ("www.linkedin.com/in/satyanadella", "satyanadella"),
    ("linkedin.com/in/satyanadella/", "satyanadella"),
    ("https://in.linkedin.com/in/satyanadella", "satyanadella"),
    ("https://uk.linkedin.com/in/satyanadella/en", "satyanadella"),
    ("https://www.linkedin.com/in/satyanadella/?trk=public_profile", "satyanadella"),
    ("https://www.linkedin.com/in/satyanadella/?originalSubdomain=in", "satyanadella"),
    ("https://www.linkedin.com/in/williamhgates/recent-activity/all/", "williamhgates"),
    ("https://www.linkedin.com/in/anne-marie-o%27neill", "anne-marie-o'neill"),
    ("https://www.linkedin.com/pub/john-doe/1/a2b/3c4", "john-doe"),
    ("satyanadella", "satyanadella"),
    ("urn:li:fsd_profile:ACoAAA1234", "ACoAAA1234"),
]

INVALID = [
    "",
    "   ",
    "https://twitter.com/in/satyanadella",
    "https://www.linkedin.com/company/microsoft/",
    "https://www.linkedin.com/school/mit/",
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/",
    "https://www.linkedin.com/in/",
    "https://www.linkedin.com/jobs/view/123",
]


@pytest.mark.parametrize("url,expected", VALID)
def test_parses_valid_urls(url, expected):
    assert parse_profile_url(url).public_identifier == expected


@pytest.mark.parametrize("url", INVALID)
def test_rejects_invalid_urls(url):
    with pytest.raises(InvalidProfileURL):
        parse_profile_url(url)


def test_canonical_url_is_normalized():
    ref = parse_profile_url("https://in.linkedin.com/in/satyanadella?trk=x")
    assert ref.canonical_url == "https://www.linkedin.com/in/satyanadella/"
