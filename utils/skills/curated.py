"""
High-value skills/aliases that O*NET often lacks or names poorly.

Used by scripts/load_curated_skills.py to upsert into skills + skill_aliases.
Aliases are conservative (no 2-letter tokens, no stems that hit prose).
"""
from __future__ import annotations

from typing import TypedDict


class CuratedSkill(TypedDict):
    name: str
    category: str
    is_hot: bool
    is_in_demand: bool
    aliases: tuple[str, ...]


# Keep this list small and JD-shaped. Prefer attaching aliases to an existing
# catalog row (loader matches on name/alias) over inserting a duplicate.
CURATED_SKILLS: tuple[CuratedSkill, ...] = (
    {
        "name": "TypeScript",
        "category": "languages",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("typescript", "type script"),
    },
    {
        "name": "Next.js",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("next.js", "nextjs", "next js"),
    },
    {
        "name": "React Native",
        "category": "mobile",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("react native", "react-native", "reactnative"),
    },
    {
        "name": "Vue.js",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("vue.js", "vuejs", "vue js"),
    },
    {
        "name": "Svelte",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": False,
        "aliases": ("svelte", "sveltekit", "svelte kit"),
    },
    {
        "name": "Tailwind CSS",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("tailwind css", "tailwind", "tailwindcss"),
    },
    {
        "name": "GraphQL",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("graphql", "graph ql"),
    },
    {
        "name": "FastAPI",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("fastapi", "fast api"),
    },
    {
        "name": "NestJS",
        "category": "web frameworks",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("nestjs", "nest.js", "nest js"),
    },
    {
        "name": "Prisma",
        "category": "databases",
        "is_hot": True,
        "is_in_demand": False,
        "aliases": ("prisma", "prisma orm"),
    },
    {
        "name": "Terraform",
        "category": "devops",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("terraform", "hashicorp terraform"),
    },
    {
        "name": "Pulumi",
        "category": "devops",
        "is_hot": True,
        "is_in_demand": False,
        "aliases": ("pulumi",),
    },
    {
        "name": "Helm",
        "category": "devops",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("helm", "helm charts"),
    },
    {
        "name": "GitHub Actions",
        "category": "devops",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("github actions", "gh actions"),
    },
    {
        "name": "Playwright",
        "category": "qa",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("playwright", "playwright test"),
    },
    {
        "name": "Cypress",
        "category": "qa",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("cypress", "cypress.io"),
    },
    {
        "name": "dbt",
        "category": "data",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("dbt", "dbt core", "data build tool"),
    },
    {
        "name": "Snowflake",
        "category": "data",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("snowflake", "snowflake db"),
    },
    {
        "name": "Databricks",
        "category": "data",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("databricks", "data bricks"),
    },
    {
        "name": "Apache Airflow",
        "category": "data",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("airflow", "apache airflow"),
    },
    {
        "name": "Apache Kafka",
        "category": "data",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("kafka", "apache kafka"),
    },
    {
        "name": "Redis",
        "category": "databases",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("redis", "redis cache"),
    },
    {
        "name": "Elasticsearch",
        "category": "databases",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("elasticsearch", "elastic search", "opensearch"),
    },
    {
        "name": "PyTorch",
        "category": "ml",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("pytorch", "py torch"),
    },
    {
        "name": "LangChain",
        "category": "ml",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("langchain", "lang chain"),
    },
    {
        "name": "Hugging Face",
        "category": "ml",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("hugging face", "huggingface"),
    },
    {
        "name": "Figma",
        "category": "design",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("figma",),
    },
    {
        "name": "Power BI",
        "category": "analytics",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("power bi", "powerbi", "microsoft power bi"),
    },
    {
        "name": "Looker",
        "category": "analytics",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("looker", "google looker", "looker studio"),
    },
    {
        "name": "Datadog",
        "category": "observability",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("datadog", "data dog"),
    },
    {
        "name": "Grafana",
        "category": "observability",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("grafana",),
    },
    {
        "name": "Prometheus",
        "category": "observability",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("prometheus",),
    },
    {
        "name": "OpenTelemetry",
        "category": "observability",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("opentelemetry", "open telemetry", "otel"),
    },
    {
        "name": "Sentry",
        "category": "observability",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("sentry", "sentry.io"),
    },
    {
        "name": "Auth0",
        "category": "security",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("auth0", "auth 0"),
    },
    {
        "name": "Okta",
        "category": "security",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("okta",),
    },
    {
        "name": "Flutter",
        "category": "mobile",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("flutter",),
    },
    {
        "name": "Kotlin",
        "category": "languages",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("kotlin",),
    },
    {
        "name": "Rust",
        "category": "languages",
        "is_hot": True,
        "is_in_demand": True,
        "aliases": ("rust", "rustlang", "rust-lang"),
    },
)
