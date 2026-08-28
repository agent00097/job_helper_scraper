"""Dictionary / alias skill matcher (longest-first scan)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

from utils.skills.catalog import SkillCatalog
from utils.skills.normalize import normalize_text
from utils.skills.sections import JobSections, extract_sections

# Ultra-generic / noisy O*NET workplace examples.
_GENERIC_ALIASES = frozenset(
    {
        "email software",
        "web browser software",
        "office suite software",
        "database software",
        "spreadsheet software",
        "word processing software",
        "presentation software",
        "calendar and scheduling software",
        "desktop communications software",
        "instant messaging software",
        "internet browser software",
        "graphics or photo imaging software",
        "video creation and editing software",
        "desktop publishing software",
        "document management software",
        "file versioning software",
        "cloud-based data access and sharing software",
        "enterprise resource planning erp software",
        "customer relationship management crm software",
        "human resources software",
        "project management software",
        "operating system software",
        "software",
        "microsoft office software",
        "microsoft windows",
        "facebook",
        "google",
        "analyze",
        "linkedin",
        "go",
        "r",
        "c",
        "management",
        "manager",
        "system",
        "systems",
        "platform",
        "application",
        "applications",
        "active",
        "cloud",
        "email",
        "mail",
        "office",
        "windows",
        "server",
        "data",
        "query",
        "language",
        "report",
        "reports",
        "analysis",
        "design",
        "test",
        "testing",
        "service",
        "services",
        "network",
        "security",
        "mobile",
        "web",
        "time",
        "user",
        "client",
        "enterprise",
        # Common English / JD verbs & nouns that collide with stripped skill names
        "calm",
        "lead",
        "post",
        "tools",
        "tool",
        "scale",
        "impact",
        "the",
        "log",
        "logs",
        "order",
        "orders",
        "risk",
        "inventory",
        "reporting",
        "deployment",
        "integration",
        "testing",
        "root",
        "cause",
        "apis",
        "api",
        "rest",
        "fast",
        "hub",
        "box",
        "match",
        "short",
        "vision",
        "partner",
        "staff",
        "engineer",
        "manager",
        "specialist",
        "operations",
        "facility",
        "federal",
        "affairs",
        "performance",
        "release",
        "change",
        "workflow",
        "workflows",
        "process",
        "processes",
        "fulfillment",
        "orchestration",
        "synchronization",
        "automation",
        "infrastructure",
        "transport",
        "customer",
        "environment",
        "environments",
        "governance",
        "compliance",
        "documentation",
        "decision",
        "decisions",
        "timeline",
        "timelines",
        "outcome",
        "outcomes",
        "pressure",
        "sound",
        "translate",
        "issues",
        "issue",
    }
)

_ALLOWED_SHORT = frozenset(
    {
        "c++",
        "c#",
        "ai",
        "ml",
        "qa",
        "ui",
        "ux",
        "sql",
        "aws",
        "gcp",
        "css",
        "php",
        "sas",
        "sap",
        "ios",
        "git",
        "ci",
        "cd",
        "k8s",
        "dbt",
        "gql",
    }
)

# Manual JD short forms. Keys resolve against catalog name, "X software",
# or an existing alias (see resolve_catalog_skill_id). Keep in sync with
# job_matcher/app/skills/alias_matcher.py.
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "salesforce software": ("salesforce", "sfdc", "sales force"),
    "salesforce": ("sfdc", "sales force"),
    "kubernetes": ("k8s",),
    "docker": ("docker compose", "dockerfile"),
    "go": ("golang", "go lang", "go programming"),
    "node.js": ("nodejs", "node js"),
    "amazon web services aws software": ("aws", "amazon web services"),
    "amazon web services": ("aws",),
    "microsoft azure software": ("azure", "microsoft azure"),
    "azure": ("microsoft azure",),
    "google cloud software": ("google cloud", "gcp", "google cloud platform", "gcloud"),
    "google cloud": ("gcp", "google cloud platform", "gcloud"),
    "react": ("react.js", "reactjs", "react js"),
    "react native": ("react-native", "reactnative"),
    "postgresql": ("postgres", "psql"),
    "mongodb": ("mongo", "mongo db"),
    "c++": ("cpp", "c plus plus", "cplusplus"),
    "c#": ("csharp", "c sharp", "c-sharp"),
    "python": ("python3", "python 3"),
    "structured query language sql": ("sql",),
    "atlassian jira": ("jira",),
    "jira": ("atlassian jira",),
    "mulesoft software": ("mulesoft", "mule soft"),
    "mulesoft": ("mule soft",),
    "typescript": ("type script",),
    "next.js": ("nextjs", "next js"),
    "vue.js": ("vuejs", "vue js"),
    "angular": ("angularjs", "angular.js"),
    "graphql": ("graph ql", "gql"),
    "terraform": ("hashicorp terraform",),
    "fastapi": ("fast api",),
    "django": ("django rest framework",),
    "ruby on rails": ("rails",),
    "spring boot": ("springboot", "spring-boot"),
    "pytorch": ("py torch",),
    "tensorflow": ("tensor flow",),
    "scikit-learn": ("sklearn", "scikit learn"),
    "apache kafka": ("kafka",),
    "kafka": ("apache kafka",),
    "redis": ("redis cache",),
    "elasticsearch": ("elastic search", "opensearch"),
    "snowflake": ("snowflake db",),
    "databricks": ("data bricks",),
    "dbt": ("dbt core", "data build tool"),
    "apache airflow": ("airflow",),
    "airflow": ("apache airflow",),
    "apache spark": ("pyspark", "py spark"),
    "power bi": ("powerbi", "microsoft power bi"),
    "github actions": ("gh actions",),
    "langchain": ("lang chain",),
    "hugging face": ("huggingface",),
    "tailwind css": ("tailwind", "tailwindcss"),
}

_TITLE_WEIGHT = 1.0
_REQ_WEIGHT = 0.90
_RESP_WEIGHT = 0.70
_BODY_WEIGHT = 0.60
_MIN_ALIAS_LEN = 3

_SOFTWARE_SUFFIX_RE = re.compile(r"\s+software$", re.IGNORECASE)
_ACRONYM_RE = re.compile(r"^[A-Z]{2,6}$")
_ACRONYM_IN_NAME_RE = re.compile(r"\b([A-Z]{2,6})\b")


@dataclass(frozen=True)
class AliasHit:
    skill_id: UUID
    skill_name: str
    alias: str
    weight: float
    in_title: bool
    source: str = "body"  # title | requirements | responsibilities | body


def _compile_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    return re.compile(rf"(?<![a-z0-9+#]){escaped}(?![a-z0-9+#])", re.IGNORECASE)


def _alias_allowed(normalized_alias: str) -> bool:
    if normalized_alias in _GENERIC_ALIASES:
        return False
    if len(normalized_alias) < _MIN_ALIAS_LEN:
        return normalized_alias in _ALLOWED_SHORT
    # Avoid ultra-generic 1-word stems.
    if " " not in normalized_alias and normalized_alias in _GENERIC_ALIASES:
        return False
    return True


def resolve_catalog_skill_id(
    key: str,
    by_norm_name: dict[str, UUID],
    alias_map: dict[str, tuple[UUID, str]],
) -> Optional[UUID]:
    """
    Map an extra-alias key onto a catalog skill.

    Tries exact normalized name, "X software" (O*NET workplace examples),
    then an already-loaded alias.
    """
    norm = (key or "").strip().lower()
    if not norm:
        return None
    if norm in by_norm_name:
        return by_norm_name[norm]
    software = f"{norm} software"
    if software in by_norm_name:
        return by_norm_name[software]
    hit = alias_map.get(norm)
    if hit:
        return hit[0]
    return None


def derived_aliases_for_name(name: str) -> list[str]:
    """
    Auto short forms from an O*NET workplace example name.
    e.g. "Structured query language SQL" -> ["SQL"]
         "MuleSoft software" -> ["MuleSoft"]
         "Google Cloud software" -> ["Google Cloud"]

    Conservative on purpose: single-token stems and random mid-name
    acronyms create massive false positives in prose JDs.
    """
    name = (name or "").strip()
    if not name:
        return []
    out: list[str] = []

    stripped = _SOFTWARE_SUFFIX_RE.sub("", name).strip()
    if stripped and stripped.lower() != name.lower():
        # Keep multi-word stems, or product-like single tokens (MuleSoft, Salesforce).
        # Skip generic category stems ("risk management", "root cause analysis").
        low_stem = stripped.lower()
        if low_stem.endswith(
            (
                " management",
                " testing",
                " analysis",
                " planning",
                " monitoring",
                " reporting",
                " processing",
            )
        ):
            stripped = ""
        tokens = stripped.split() if stripped else []
        if len(tokens) >= 2:
            out.append(stripped)
        elif len(tokens) == 1:
            tok = tokens[0]
            # CamelCase / internal caps only (MuleSoft, PostgreSQL) — not "Modeling".
            looks_product = any(ch.islower() for ch in tok) and any(
                ch.isupper() for ch in tok[1:]
            )
            if looks_product:
                out.append(tok)

    # Trailing acronym only on short names (SQL, AWS, JIRA). Long product names
    # often end in unrelated ALLCAPS tokens (e.g. "... Inspection AI").
    parts = name.replace(",", " ").split()
    if parts:
        last = parts[-1]
        effective_parts = parts
        if last.lower() == "software" and len(parts) >= 2:
            last = parts[-2]
            effective_parts = parts[:-1]
        is_acronym = _ACRONYM_RE.match(last) or (
            last.isupper() and 2 <= len(last) <= 6 and last.isalpha()
        )
        if is_acronym and 2 <= len(effective_parts) <= 5:
            out.append(last)

    # Well-known "Vendor PRODUCT" pattern: last token acronym after a vendor word.
    if len(parts) == 2 and _ACRONYM_RE.match(parts[1]):
        out.append(parts[1])

    seen: set[str] = set()
    uniq: list[str] = []
    for a in out:
        key = a.strip().lower()
        if not key or key in seen:
            continue
        if not _alias_allowed(key):
            continue
        seen.add(key)
        uniq.append(a.strip())
    return uniq


class AliasMatcher:
    """Longest-alias-first matcher over a skill catalog."""

    def __init__(self, catalog: SkillCatalog):
        self.catalog = catalog
        by_norm_name = {s.normalized_name: s.skill_id for s in catalog.skills.values()}

        alias_map: dict[str, tuple[UUID, str]] = dict(catalog.aliases)

        # Manual extras. Keys often omit the O*NET " software" suffix.
        for skill_norm, extras in _EXTRA_ALIASES.items():
            skill_id = resolve_catalog_skill_id(skill_norm, by_norm_name, alias_map)
            if skill_id is None:
                continue
            for extra in extras:
                key = extra.strip().lower()
                if key and key not in alias_map:
                    alias_map[key] = (skill_id, extra)

        # Auto-derived short forms. Collect first, then only keep UNIQUE keys
        # (if two skills both want "SQL"-like collisions, drop the ambiguous ones
        # unless already claimed by an explicit catalog/manual alias).
        derived_claims: dict[str, list[tuple[UUID, str]]] = {}
        for skill in catalog.skills.values():
            for derived in derived_aliases_for_name(skill.name):
                key = derived.lower()
                if key in alias_map or not _alias_allowed(key):
                    continue
                derived_claims.setdefault(key, []).append((skill.skill_id, derived))
        for key, claims in derived_claims.items():
            if len(claims) != 1:
                continue
            skill_id, derived = claims[0]
            alias_map[key] = (skill_id, derived)

        entries: list[tuple[str, UUID, str, re.Pattern[str]]] = []
        for norm_alias, (skill_id, display_alias) in alias_map.items():
            if not _alias_allowed(norm_alias):
                continue
            entries.append(
                (norm_alias, skill_id, display_alias, _compile_pattern(norm_alias))
            )
        entries.sort(key=lambda e: len(e[0]), reverse=True)
        self._entries = entries

    def match(self, title: str, description: str = "") -> list[AliasHit]:
        sections = extract_sections(description or "")
        return self.match_sections(title or "", sections, fallback_body=description or "")

    def match_sections(
        self,
        title: str,
        sections: JobSections,
        *,
        fallback_body: str = "",
    ) -> list[AliasHit]:
        best: dict[UUID, AliasHit] = {}

        self._scan(normalize_text(title), weight=_TITLE_WEIGHT, in_title=True, source="title", into=best)

        if sections.structured:
            if sections.requirements:
                self._scan(
                    normalize_text(sections.requirements),
                    weight=_REQ_WEIGHT,
                    in_title=False,
                    source="requirements",
                    into=best,
                )
            if sections.responsibilities:
                self._scan(
                    normalize_text(sections.responsibilities),
                    weight=_RESP_WEIGHT,
                    in_title=False,
                    source="responsibilities",
                    into=best,
                )
        else:
            self._scan(
                normalize_text(fallback_body),
                weight=_BODY_WEIGHT,
                in_title=False,
                source="body",
                into=best,
            )

        return sorted(best.values(), key=lambda h: (-h.weight, h.skill_name.lower()))

    def _scan(
        self,
        text: str,
        *,
        weight: float,
        in_title: bool,
        source: str,
        into: dict[UUID, AliasHit],
    ) -> None:
        if not text:
            return
        occupied: list[tuple[int, int]] = []

        for norm_alias, skill_id, display_alias, pattern in self._entries:
            for m in pattern.finditer(text):
                span = (m.start(), m.end())
                if _overlaps(span, occupied):
                    continue
                occupied.append(span)
                skill = self.catalog.get(skill_id)
                name = skill.name if skill else display_alias
                prev = into.get(skill_id)
                if prev is None or weight > prev.weight:
                    into[skill_id] = AliasHit(
                        skill_id=skill_id,
                        skill_name=name,
                        alias=display_alias,
                        weight=weight,
                        in_title=in_title or (prev.in_title if prev else False),
                        source=source,
                    )
                break


def _overlaps(span: tuple[int, int], occupied: Iterable[tuple[int, int]]) -> bool:
    a, b = span
    for c, d in occupied:
        if a < d and c < b:
            return True
    return False
