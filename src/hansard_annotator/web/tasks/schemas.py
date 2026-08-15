"""Typed, allowlisted batch selection criteria."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SelectionCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None
    years: list[int] = Field(default_factory=list, max_length=50)
    minimum_words: int = Field(default=50, ge=0, le=100_000)
    maximum_words: int | None = Field(default=None, ge=0, le=100_000)
    exclude_orphan_continuations: bool = True
    question_time_hint: bool | None = None
    procedural_hint: bool | None = None
    ceremonial_hint: bool | None = None
    minimum_interruption_count: int = Field(default=0, ge=0, le=10_000)
    limit: int | None = Field(default=None, ge=1, le=50_000)
    seed: int = 20260724
    source_domain_code: str | None = Field(
        default=None, pattern=r"^AU(?:\d{2}|_OTHER_REVIEW)$"
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> SelectionCriteria:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if (
            self.maximum_words is not None
            and self.maximum_words < self.minimum_words
        ):
            raise ValueError("maximum_words must be at least minimum_words")
        if any(year < 1901 or year > 9999 for year in self.years):
            raise ValueError("years must be between 1901 and 9999")
        self.years = sorted(set(self.years))
        return self
