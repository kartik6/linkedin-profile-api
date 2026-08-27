"""Response schema for the API.

Every strategy normalizes into these models. The API never returns raw
LinkedIn payloads, so a change inside LinkedIn does not change our contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------


class Date(Base):
    """A partial date. LinkedIn often gives a year only, or a year and month."""

    year: int | None = None
    month: int | None = None
    day: int | None = None
    text: str | None = Field(
        default=None, description="Human readable form, for example 'May 2021'."
    )


class DateRange(Base):
    start: Date | None = None
    end: Date | None = None
    is_current: bool = False
    duration_months: int | None = Field(
        default=None, description="Whole months between start and end. Null if start is absent."
    )
    text: str | None = Field(
        default=None, description="Human readable range, for example 'May 2021 - Present'."
    )


class ImageArtifact(Base):
    """One size of a LinkedIn image."""

    width: int | None = None
    height: int | None = None
    url: str


class Image(Base):
    """A LinkedIn image in every size the payload offers.

    LinkedIn splits an image into a root URL and a list of path segments.
    We join them, so each artifact holds a URL you can fetch directly.
    """

    url: str | None = Field(default=None, description="Largest artifact. Use this by default.")
    artifacts: list[ImageArtifact] = Field(default_factory=list)
    expires_at: datetime | None = Field(
        default=None, description="Signed media URLs expire. Re-fetch the profile after this time."
    )


class Location(Base):
    full: str | None = None
    city: str | None = None
    country: str | None = None
    country_code: str | None = None


class Company(Base):
    name: str | None = None
    urn: str | None = None
    linkedin_url: str | None = None
    logo: Image | None = None
    industry: str | None = None
    staff_count: int | None = None


class School(Base):
    name: str | None = None
    urn: str | None = None
    linkedin_url: str | None = None
    logo: Image | None = None


# --------------------------------------------------------------------------
# Profile sections
# --------------------------------------------------------------------------


class EmploymentType(str, Enum):
    full_time = "FULL_TIME"
    part_time = "PART_TIME"
    contract = "CONTRACT"
    internship = "INTERNSHIP"
    freelance = "FREELANCE"
    self_employed = "SELF_EMPLOYED"
    apprenticeship = "APPRENTICESHIP"
    seasonal = "SEASONAL"
    other = "OTHER"


class Experience(Base):
    title: str | None = None
    company: Company | None = None
    employment_type: EmploymentType | None = None
    location: str | None = None
    workplace_type: Literal["ON_SITE", "REMOTE", "HYBRID"] | None = None
    description: str | None = None
    date_range: DateRange | None = None
    skills: list[str] = Field(default_factory=list)


class Education(Base):
    school: School | None = None
    degree: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Skill(Base):
    name: str
    endorsement_count: int | None = None
    insights: list[str] = Field(
        default_factory=list, description="For example 'Passed LinkedIn skill assessment'."
    )


class Certification(Base):
    name: str | None = None
    authority: str | None = None
    license_number: str | None = None
    url: str | None = None
    logo: Image | None = None
    issued_on: Date | None = None
    expires_on: Date | None = None


class Language(Base):
    name: str
    proficiency: str | None = None


class Project(Base):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    date_range: DateRange | None = None
    members: list[str] = Field(default_factory=list)


class Publication(Base):
    name: str | None = None
    publisher: str | None = None
    description: str | None = None
    url: str | None = None
    published_on: Date | None = None
    authors: list[str] = Field(default_factory=list)


class Honor(Base):
    title: str | None = None
    issuer: str | None = None
    description: str | None = None
    issued_on: Date | None = None


class Volunteer(Base):
    role: str | None = None
    organization: str | None = None
    cause: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Course(Base):
    name: str | None = None
    number: str | None = None


class Patent(Base):
    title: str | None = None
    number: str | None = None
    description: str | None = None
    url: str | None = None
    issued_on: Date | None = None
    inventors: list[str] = Field(default_factory=list)


class Organization(Base):
    name: str | None = None
    position: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class TestScore(Base):
    name: str | None = None
    score: str | None = None
    description: str | None = None
    taken_on: Date | None = None


class Profile(Base):
    """The full profile. Every field is optional, because visibility changes
    with the privacy settings of the profile owner and with our login state.
    """

    public_identifier: str | None = None
    urn: str | None = Field(default=None, description="Stable LinkedIn member URN.")
    profile_url: str | None = None

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    pronouns: str | None = None
    headline: str | None = None
    about: str | None = None
    industry: str | None = None
    location: Location | None = None

    profile_picture: Image | None = None
    background_picture: Image | None = None

    follower_count: int | None = None
    connection_count: int | None = None
    connection_degree: str | None = None
    open_to_work: bool | None = None
    is_hiring: bool | None = None
    is_premium: bool | None = None
    is_influencer: bool | None = None
    is_verified: bool | None = None

    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    volunteering: list[Volunteer] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)
    patents: list[Patent] = Field(default_factory=list)
    organizations: list[Organization] = Field(default_factory=list)
    test_scores: list[TestScore] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class Meta(Base):
    """Tells the caller how we got the data and how complete it is."""

    strategy: str = Field(description="Which fetch strategy produced the result.")
    strategies_tried: list[str] = Field(default_factory=list)
    cached: bool = False
    fetched_at: datetime
    duration_ms: int
    completeness: float = Field(
        ge=0.0, le=1.0, description="Share of the core sections that came back with data."
    )
    partial: bool = Field(
        default=False, description="True when at least one core section is empty."
    )
    warnings: list[str] = Field(default_factory=list)


class ProfileResponse(Base):
    profile: Profile
    meta: Meta


class BatchItem(Base):
    url: str
    ok: bool
    profile: Profile | None = None
    meta: Meta | None = None
    error: dict[str, Any] | None = None


class BatchResponse(Base):
    results: list[BatchItem]
    requested: int
    succeeded: int
    failed: int


class ErrorResponse(Base):
    error: str = Field(description="Stable machine readable code.")
    message: str
    detail: Any | None = None
