"""The normalizer, checked against real captured LinkedIn payloads.

Each assertion here corresponds to a field name we verified by hand. Where a
field is absent, the test says so and explains why, so a future reader does not
mistake a LinkedIn limitation for a bug in this code.
"""

from __future__ import annotations

from app.linkedin.entities import EntityPool
from app.linkedin.normalize import (
    completeness,
    from_entity_pool,
    merge_profiles,
    missing_sections,
)
from app.models import Profile, Skill
from tests.conftest import PROFILE_URN, PUBLIC_ID


class TestTopCard:
    def test_reads_identity(self, full_pool, ref):
        p = from_entity_pool(full_pool, ref)
        assert p.first_name == "Ada"
        assert p.last_name == "Lovelace"
        assert p.full_name == "Ada Lovelace"
        assert p.public_identifier == PUBLIC_ID
        assert p.urn == PROFILE_URN

    def test_reads_headline_and_about(self, full_pool, ref):
        p = from_entity_pool(full_pool, ref)
        assert p.headline.startswith("Principal Engineer")
        assert "systems" in p.about

    def test_reads_pronouns_from_the_union(self, full_pool, ref):
        """Verified shape: {"pronounUnion": {"standardizedPronoun": "HE_HIM"}}."""
        assert from_entity_pool(full_pool, ref).pronouns == "he/him"

    def test_country_code_is_flat_not_nested(self, full_pool, ref):
        """Verified shape: {"location": {"countryCode": "IN"}}.

        There is no basicLocation wrapper. The code looked for one and found
        nothing until we checked the real payload.
        """
        assert from_entity_pool(full_pool, ref).location.country_code == "IN"

    def test_location_name_is_absent_from_this_endpoint(self, full_pool, ref):
        """LinkedIn sends geoLocation as a bare geoUrn with no display name.

        This is a capability limit, not a parsing failure. Resolving it needs a
        separate call we have not mapped.
        """
        assert from_entity_pool(full_pool, ref).location.full is None


class TestImages:
    def test_builds_every_size_from_root_url_and_segment(self, full_pool, ref):
        picture = from_entity_pool(full_pool, ref).profile_picture
        assert [a.width for a in picture.artifacts] == [100, 200, 400, 800]
        prefix = "https://media.licdn.com/dms/image/"
        assert all(a.url.startswith(prefix) for a in picture.artifacts)

    def test_default_url_is_the_largest(self, full_pool, ref):
        picture = from_entity_pool(full_pool, ref).profile_picture
        assert picture.url == picture.artifacts[-1].url

    def test_expiry_is_read_from_the_signed_url(self, full_pool, ref):
        assert from_entity_pool(full_pool, ref).profile_picture.expires_at is not None

    def test_background_picture_is_found_too(self, full_pool, ref):
        assert from_entity_pool(full_pool, ref).background_picture is not None


class TestSections:
    def test_every_section_arrives(self, full_pool, ref):
        p = from_entity_pool(full_pool, ref)
        assert len(p.experience) == 11
        assert len(p.education) == 2
        assert len(p.skills) == 20
        assert len(p.certifications) == 12
        assert len(p.projects) == 1

    def test_experience_reads_title_company_and_dates(self, full_pool, ref):
        roles = from_entity_pool(full_pool, ref).experience
        assert all(r.title for r in roles)
        assert any(r.company and r.company.name for r in roles)
        assert any(r.date_range and r.date_range.start for r in roles)

    def test_a_current_role_has_no_end_date(self, full_pool, ref):
        roles = from_entity_pool(full_pool, ref).experience
        current = [r for r in roles if r.date_range and r.date_range.is_current]
        assert current
        assert all(r.date_range.end is None for r in current)

    def test_location_is_present_on_some_roles_only(self, full_pool, ref):
        """Verified: locationName appeared on 5 of 11 real positions."""
        roles = from_entity_pool(full_pool, ref).experience
        assert any(r.location for r in roles)
        assert any(not r.location for r in roles)

    def test_descriptions_are_absent_from_this_endpoint(self, full_pool, ref):
        """Verified: 0 of 11 positions carried a description field.

        profilePositions does not return them at all. Documented as a limit.
        """
        assert all(r.description is None for r in from_entity_pool(full_pool, ref).experience)

    def test_education_reads_school_degree_field_and_grade(self, full_pool, ref):
        first = from_entity_pool(full_pool, ref).education[0]
        assert first.school and first.school.name
        assert first.degree
        assert first.field_of_study
        assert first.grade

    def test_certifications_read_authority_and_licence(self, full_pool, ref):
        certs = from_entity_pool(full_pool, ref).certifications
        assert all(c.name for c in certs)
        assert any(c.authority for c in certs)
        assert any(c.license_number for c in certs)

    def test_skills_are_deduplicated_by_name(self, sections, ref):
        pool = EntityPool.from_payload(sections["profileSkills"])
        names = [s.name for s in from_entity_pool(pool, ref).skills]
        assert len(names) == len(set(names))

    def test_empty_sections_yield_empty_lists_not_errors(self, full_pool, ref):
        """A section the person has nothing in returns a valid empty collection."""
        p = from_entity_pool(full_pool, ref)
        assert p.languages == []
        assert p.honors == []
        assert p.patents == []


class TestResilience:
    def test_empty_pool_does_not_raise(self, ref):
        p = from_entity_pool(EntityPool(), ref)
        assert p.public_identifier == PUBLIC_ID
        assert p.experience == []

    def test_a_broken_entity_costs_one_section_not_the_profile(self, top_card, sections, ref):
        broken = dict(sections["profilePositions"])
        broken["included"] = "this should be a list"
        pool = EntityPool.from_payload(top_card)
        pool.merge(EntityPool.from_payload(broken))
        pool.merge(EntityPool.from_payload(sections["profileSkills"]))

        p = from_entity_pool(pool, ref)
        assert p.experience == []
        assert p.full_name == "Ada Lovelace"
        assert len(p.skills) == 20

    def test_matches_both_linkedin_namespaces(self, ref):
        """LinkedIn ships `voyager.identity.*` and `voyager.dash.identity.*`."""
        pool = EntityPool.from_payload({"included": [
            {"$type": "com.linkedin.voyager.identity.profile.Position",
             "entityUrn": "urn:a", "title": "Legacy namespace"},
            {"$type": "com.linkedin.voyager.dash.identity.profile.Position",
             "entityUrn": "urn:b", "title": "Dash namespace"},
        ]})
        assert [e.title for e in from_entity_pool(pool, ref).experience] == [
            "Legacy namespace", "Dash namespace",
        ]


class TestScoring:
    def test_a_real_profile_scores_full(self, full_pool, ref):
        assert completeness(from_entity_pool(full_pool, ref)) == 1.0

    def test_empty_profile_scores_zero(self):
        assert completeness(Profile()) == 0.0

    def test_only_universal_sections_count(self):
        """Languages and patents are not scored. Most real profiles have none."""
        from app.linkedin.normalize import CORE_SECTIONS

        assert set(CORE_SECTIONS) == {"experience", "education", "skills"}

    def test_missing_sections_are_named(self, ref):
        p = from_entity_pool(EntityPool(), ref)
        assert set(missing_sections(p)) == {"experience", "education", "skills"}


class TestMerge:
    def test_base_wins_and_gaps_are_filled(self):
        base = Profile(first_name="Ada", headline="Engineer")
        extra = Profile(first_name="Someone else", last_name="Lovelace", about="Bio")
        merged = merge_profiles(base, extra)
        assert merged.first_name == "Ada"
        assert merged.last_name == "Lovelace"
        assert merged.about == "Bio"

    def test_empty_list_counts_as_a_gap(self):
        merged = merge_profiles(Profile(skills=[]), Profile(skills=[Skill(name="Rust")]))
        assert merged.skills[0].name == "Rust"
