"""The normalizers are where most of the risk lives, so most of the tests are here."""

from __future__ import annotations

from app.linkedin.entities import EntityPool
from app.linkedin.normalize import (
    completeness,
    from_entity_pool,
    from_profile_view,
    merge_profiles,
    missing_sections,
)
from app.models import Profile


class TestProfileView:
    def test_reads_the_top_card(self, profile_view, ref):
        p = from_profile_view(profile_view, ref)
        assert p.full_name == "Ada Lovelace"
        assert p.first_name == "Ada"
        assert p.public_identifier == "adalovelace"
        assert p.headline.startswith("Principal Engineer")
        assert p.about.startswith("I build systems")
        assert p.industry == "Software Development"

    def test_reads_the_location(self, profile_view, ref):
        location = from_profile_view(profile_view, ref).location
        assert location.full == "Bengaluru, Karnataka, India"
        assert location.city == "Bengaluru"
        assert location.country == "India"
        assert location.country_code == "IN"

    def test_joins_image_urls_and_keeps_every_size(self, profile_view, ref):
        picture = from_profile_view(profile_view, ref).profile_picture
        assert [a.width for a in picture.artifacts] == [100, 400, 800]
        assert picture.url.endswith("800_800/0/16?e=1767225600&v=beta&t=cc")
        assert picture.url.startswith("https://media.licdn.com/")
        assert picture.expires_at is not None

    def test_reads_experience_with_dates_and_company(self, profile_view, ref):
        first = from_profile_view(profile_view, ref).experience[0]
        assert first.title == "Principal Engineer"
        assert first.company.name == "Analytical Engines"
        assert first.company.linkedin_url == "https://www.linkedin.com/company/analytical-engines/"
        assert first.company.logo.url.startswith("https://media.licdn.com/")
        assert first.date_range.is_current is True
        assert first.date_range.start.year == 2021

    def test_closed_range_gets_a_duration(self, profile_view, ref):
        second = from_profile_view(profile_view, ref).experience[1]
        assert second.date_range.is_current is False
        assert second.date_range.duration_months == 40
        assert second.date_range.text == "January 2018 - April 2021"

    def test_reads_every_other_section(self, profile_view, ref):
        p = from_profile_view(profile_view, ref)
        assert [s.name for s in p.skills] == [
            "Distributed Systems", "Rust", "PostgreSQL", "Kubernetes",
        ]
        assert p.certifications[0].license_number == "AWS-PSA-99182"
        assert p.certifications[0].expires_on.year == 2026
        assert [lang.proficiency for lang in p.languages] == [
            "Native Or Bilingual", "Professional Working",
        ]
        assert p.education[0].grade == "8.9 CGPA"
        assert p.projects[0].name == "OpenLedger"
        assert p.publications[0].publisher == "ACM Queue"
        assert p.honors[0].issuer == "IIT Bombay"
        assert p.volunteering[0].organization == "Code for India"
        assert p.courses[0].number == "CS614"

    def test_empty_document_does_not_raise(self, ref):
        p = from_profile_view({}, ref)
        assert p.public_identifier == "adalovelace"
        assert p.experience == []

    def test_broken_section_does_not_lose_the_rest(self, profile_view, ref):
        profile_view["positionView"] = {"elements": "this should be a list"}
        p = from_profile_view(profile_view, ref)
        assert p.experience == []
        assert p.full_name == "Ada Lovelace"
        assert len(p.skills) == 4


class TestEntityPool:
    def test_reads_the_top_card(self, dash_profile, ref):
        p = from_entity_pool(EntityPool.from_payload(dash_profile), ref)
        assert p.full_name == "Ada Lovelace"
        assert p.follower_count == 18422
        assert p.is_premium is True
        assert p.open_to_work is True
        assert p.location.country_code == "IN"

    def test_follows_a_urn_reference_to_the_company(self, dash_profile, ref):
        p = from_entity_pool(EntityPool.from_payload(dash_profile), ref)
        company = p.experience[0].company
        assert company.name == "Analytical Engines"
        assert company.staff_count == 2400
        assert company.logo is not None

    def test_follows_a_urn_reference_to_the_industry(self, dash_profile, ref):
        p = from_entity_pool(EntityPool.from_payload(dash_profile), ref)
        assert p.industry == "Software Development"

    def test_reads_employment_and_workplace_type(self, dash_profile, ref):
        first = from_entity_pool(EntityPool.from_payload(dash_profile), ref).experience[0]
        assert first.employment_type.value == "FULL_TIME"
        assert first.workplace_type == "HYBRID"

    def test_removes_duplicate_skills(self, dash_profile, ref):
        p = from_entity_pool(EntityPool.from_payload(dash_profile), ref)
        assert [s.name for s in p.skills] == ["Distributed Systems", "Rust"]
        assert p.skills[0].endorsement_count == 42

    def test_matches_both_namespaces(self, ref):
        """The old fs_ names and the new dash names must both resolve."""
        pool = EntityPool.from_payload(
            {
                "included": [
                    {
                        "$type": "com.linkedin.voyager.identity.profile.Position",
                        "entityUrn": "urn:a",
                        "title": "Legacy namespace role",
                    },
                    {
                        "$type": "com.linkedin.voyager.dash.identity.profile.Position",
                        "entityUrn": "urn:b",
                        "title": "Dash namespace role",
                    },
                ]
            }
        )
        titles = [e.title for e in from_entity_pool(pool, ref).experience]
        assert titles == ["Legacy namespace role", "Dash namespace role"]

    def test_empty_pool_does_not_raise(self, ref):
        p = from_entity_pool(EntityPool(), ref)
        assert p.public_identifier == "adalovelace"


class TestScoring:
    def test_full_profile_scores_one(self, profile_view, ref):
        assert completeness(from_profile_view(profile_view, ref)) == 1.0

    def test_empty_profile_scores_zero(self):
        assert completeness(Profile()) == 0.0

    def test_missing_sections_are_named(self, ref):
        p = from_profile_view({"profile": {"firstName": "Ada"}}, ref)
        assert set(missing_sections(p)) == {
            "experience", "education", "skills", "certifications", "languages",
        }


class TestMerge:
    def test_base_wins_and_gaps_get_filled(self):
        base = Profile(first_name="Ada", headline="Engineer")
        extra = Profile(first_name="Someone else", last_name="Lovelace", about="Bio")
        merged = merge_profiles(base, extra)
        assert merged.first_name == "Ada"
        assert merged.last_name == "Lovelace"
        assert merged.about == "Bio"

    def test_empty_list_counts_as_a_gap(self):
        from app.models import Skill

        base = Profile(skills=[])
        extra = Profile(skills=[Skill(name="Rust")])
        assert merge_profiles(base, extra).skills[0].name == "Rust"
