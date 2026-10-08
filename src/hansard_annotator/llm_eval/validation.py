"""Provider-independent validation for model annotation payloads."""

from __future__ import annotations

from hansard_annotator.llm_eval.models import StructuredPrediction


class InvalidPrediction(ValueError):
    """A model returned data that is not a valid annotation."""


def validate_prediction(raw: object, domain_codes: tuple[str, ...]) -> StructuredPrediction:
    if not isinstance(raw, dict):
        raise InvalidPrediction("response is not an object")
    allowed = {
        "is_non_policy",
        "primary_australian_domain",
        "secondary_australian_domains",
        "reasoning",
    }
    if set(raw) != allowed:
        raise InvalidPrediction("response fields do not match the annotation schema")
    non_policy = raw["is_non_policy"]
    primary = raw["primary_australian_domain"]
    secondary = raw["secondary_australian_domains"]
    reasoning = raw["reasoning"]
    if not isinstance(non_policy, bool):
        raise InvalidPrediction("is_non_policy must be boolean")
    if primary is not None and (not isinstance(primary, str) or primary not in domain_codes):
        raise InvalidPrediction("primary domain is unknown")
    if (
        not isinstance(secondary, list)
        or len(secondary) > 2
        or any(not isinstance(item, str) for item in secondary)
    ):
        raise InvalidPrediction("secondary domains must be a list of at most two codes")
    secondary_codes = tuple(secondary)
    if len(set(secondary_codes)) != len(secondary_codes):
        raise InvalidPrediction("secondary domains must be unique")
    if any(code not in domain_codes or code == "AU_OTHER_REVIEW" for code in secondary_codes):
        raise InvalidPrediction("secondary domain is unknown or disallowed")
    if not isinstance(reasoning, str) or len(reasoning) > 1000:
        raise InvalidPrediction("reasoning must be a short string")
    if non_policy and (primary is not None or secondary_codes):
        raise InvalidPrediction("non-policy predictions cannot contain policy domains")
    if not non_policy and primary is None:
        raise InvalidPrediction("policy predictions require a primary domain")
    if primary in secondary_codes:
        raise InvalidPrediction("primary domain cannot also be secondary")
    return StructuredPrediction(non_policy, primary, secondary_codes, reasoning)
