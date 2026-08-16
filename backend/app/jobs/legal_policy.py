import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class Jurisdiction(StrEnum):
    UNITED_STATES = "US"
    INDIA = "IN"


@dataclass(frozen=True)
class ProtectedCharacteristicRule:
    jurisdiction: Jurisdiction
    category: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class PolicyMatch:
    jurisdiction: Jurisdiction
    category: str


_PEOPLE = r"(?:candidate|candidates|applicant|applicants|person|people|professional|professionals|worker|workers|employee|employees|hire|hires)"
_RACE_VALUES = r"(?:black|white|caucasian|african american|asian|hispanic|latino|latina|latinx|arab|middle eastern|native american|indigenous|pacific islander)"
_RELIGION_VALUES = r"(?:hindu|muslim|islamic|christian|catholic|protestant|sikh|buddhist|jain|jewish|jew|atheist)"
_SEX_GENDER_VALUES = r"(?:male|female|man|men|woman|women|cisgender|transgender|nonbinary|non binary|gay|lesbian|bisexual|queer)"
_CASTE_VALUES = r"(?:dalit|adivasi|brahmin|scheduled caste|scheduled tribe|upper caste|lower caste|other backward class|obc)"


PROTECTED_CHARACTERISTIC_RULES: tuple[ProtectedCharacteristicRule, ...] = (
    # United States: EEOC-enforced categories and common direct values/proxies.
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "race_color_ethnicity",
        (
            r"\b(?:race|racial|ethnicity|ethnic background|skin color|complexion|ancestry)\b",
            rf"\b{_RACE_VALUES}\s+{_PEOPLE}\b",
            rf"\b{_PEOPLE}\s+(?:who are\s+|that are\s+)?{_RACE_VALUES}\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "religion",
        (
            r"\b(?:religion|religious belief|faith affiliation|creed)\b",
            rf"\b{_RELIGION_VALUES}\s+{_PEOPLE}\b",
            rf"\b{_PEOPLE}\s+(?:who are\s+|that are\s+)?{_RELIGION_VALUES}\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "sex_gender_pregnancy",
        (
            r"\b(?:sex|gender|gender identity|sexual orientation|pregnancy|pregnant|maternity|childbirth)\b",
            rf"\b{_SEX_GENDER_VALUES}\s+{_PEOPLE}\b",
            rf"\b{_PEOPLE}\s+(?:who are\s+|that are\s+)?{_SEX_GENDER_VALUES}\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "national_origin",
        (
            r"\b(?:national origin|nationality|country of origin|birthplace|place of birth|immigrant background|foreign accent)\b",
            r"\bnative english speaker(?:s)?\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "age",
        (
            r"\b(?:age|age range|date of birth|year of birth)\b",
            rf"\b(?:young|youthful|digital native|under forty|under 40|over forty|over 40)\s+(?:and\s+\w+\s+)?{_PEOPLE}\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "disability_genetic_information",
        (
            r"\b(?:disability|disabled|medical history|family medical history|genetic information)\b",
            rf"\b(?:able bodied|physically fit)\s+{_PEOPLE}\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.UNITED_STATES,
        "veteran_or_family_status",
        (
            r"\b(?:veteran status|military status|marital status|family status)\b",
            rf"\b(?:unmarried|single|married|childless)\s+{_PEOPLE}\b",
        ),
    ),
    # India: constitutional categories, transgender protections, and caste proxies.
    ProtectedCharacteristicRule(
        Jurisdiction.INDIA,
        "religion_race_caste_sex_place_of_birth",
        (
            r"\b(?:religion|race|racial|caste|sex|place of birth|birthplace)\b",
            rf"\b{_CASTE_VALUES}\s+{_PEOPLE}\b",
            rf"\b{_RELIGION_VALUES}\s+{_PEOPLE}\b",
        ),
    ),
    ProtectedCharacteristicRule(
        Jurisdiction.INDIA,
        "transgender_status",
        (r"\b(?:transgender status|transgender person|hijra)\b",),
    ),
)


WORK_AUTHORIZATION_PATTERNS: tuple[str, ...] = (
    r"\bwork auth(?:orization|orisation)\b",
    r"\b(?:authorized|authorised|eligible|permitted) to work\b",
    r"\bright to work\b",
    r"\bwork permit\b",
    r"\bemployment (?:authorization|authorisation|eligibility|eligible)\b",
    r"\blegally (?:employable|eligible|permitted|authorized|authorised)\b",
    r"\bvisa(?: status| sponsorship)?\b",
    r"\bsponsorship\b",
    r"\bgreen card\b",
    r"\bpermanent resident\b",
    r"\b(?:citizen|citizenship)\b",
    r"\bimmigration status\b",
)


def normalize_policy_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("_", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", normalized)).strip()


class ScorecardLegalPolicy:
    """Conservative union of hiring-safety rules for supported jurisdictions."""

    def __init__(
        self,
        protected_rules: tuple[ProtectedCharacteristicRule, ...],
        work_authorization_patterns: tuple[str, ...],
    ) -> None:
        self._protected_rules = protected_rules
        self._work_authorization_patterns = work_authorization_patterns

    def protected_characteristic_match(self, value: str) -> PolicyMatch | None:
        normalized = normalize_policy_text(value)
        for rule in self._protected_rules:
            if any(re.search(pattern, normalized) for pattern in rule.patterns):
                return PolicyMatch(rule.jurisdiction, rule.category)
        return None

    def refers_to_work_authorization(self, value: str) -> bool:
        normalized = normalize_policy_text(value)
        return any(
            re.search(pattern, normalized)
            for pattern in self._work_authorization_patterns
        )


DEFAULT_SCORECARD_LEGAL_POLICY = ScorecardLegalPolicy(
    PROTECTED_CHARACTERISTIC_RULES,
    WORK_AUTHORIZATION_PATTERNS,
)
