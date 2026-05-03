#!/usr/bin/env python3
"""
Parse a raw email (.eml / raw message text), extract job links from the HTML part,
and publish one message per job to RabbitMQ.

Usage examples:

  python email_to_rabbit.py --raw-file bloomberry.eml --dry-run

  python email_to_rabbit.py \
    --raw-file bloomberry.eml \
    --publish \
    --rabbit-host 127.0.0.1 \
    --rabbit-port 5672 \
    --rabbit-user job_publisher \
    --rabbit-pass 'your-password' \
    --rabbit-vhost jobs \
    --queue job_scrape_requests

  # Same env vars as the Kubernetes worker (optional; CLI flags override):
  #   export RABBITMQ_HOST=127.0.0.1 RABBITMQ_PORT=5672
  #   export RABBITMQ_USER=job_worker RABBITMQ_PASSWORD='...' RABBITMQ_VHOST=jobs

You can also paste the raw email via stdin:

  python email_to_rabbit.py --dry-run < bloomberry.eml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from typing import List, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from bs4 import BeautifulSoup

try:
    import pika
except ImportError:
    pika = None


JOB_HOST_HINTS = (
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "myworkdayjobs.com",
    "icims.com",
    "smartrecruiters.com",
    "jobvite.com",
    "ycombinator.com",
    "rippling.com",
    "teamtailor.com",
    "personio.de",
    "catsone.com",
    "dejobs.org",
    "mayoclinic.org",
    "hubspot.com/careers",
    "lattice.com/job",
    "bitwarden.com/careers",
    "coinbase.com/careers",
    "toasttab.com/jobs",
    "samsara.com/company/careers",
    "motional.com/open-positions",
)

IGNORE_TEXT_HINTS = (
    "unsubscribe",
    "linkedin post",
    "like or repost",
    "recommend them to subscribe",
)

TRACKING_QUERY_PREFIXES = (
    "utm_",
    "trk",
    "trkinfo",
    "ref",
    "source",
    "mc_",
    "vero_",
)


@dataclass
class JobEntry:
    rank: Optional[int]
    title: str
    url: str
    location: Optional[str]
    company: Optional[str]
    salary: Optional[str]
    summary: Optional[str]
    email_subject: Optional[str]
    email_sender: Optional[str]
    source: str = "email_raw_manual"


def _rabbit_arg_defaults() -> dict:
    """Align with workers/rabbitmq_settings.py env names for local publish + port-forward."""
    return {
        "host": os.environ.get("RABBITMQ_HOST", "127.0.0.1"),
        "port": int(os.environ.get("RABBITMQ_PORT", "5672")),
        "user": os.environ.get("RABBITMQ_USER", "guest"),
        "password": os.environ.get("RABBITMQ_PASSWORD", "guest"),
        "vhost": os.environ.get("RABBITMQ_VHOST", "/"),
    }


def parse_args() -> argparse.Namespace:
    d = _rabbit_arg_defaults()
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-file", help="Path to raw email file (.eml or pasted raw message)")
    parser.add_argument("--dry-run", action="store_true", help="Print extracted jobs only")
    parser.add_argument("--publish", action="store_true", help="Publish extracted jobs to RabbitMQ")

    parser.add_argument("--rabbit-host", default=d["host"], help="Default: RABBITMQ_HOST or 127.0.0.1")
    parser.add_argument("--rabbit-port", type=int, default=d["port"], help="Default: RABBITMQ_PORT or 5672")
    parser.add_argument("--rabbit-user", default=d["user"], help="Default: RABBITMQ_USER or guest")
    parser.add_argument("--rabbit-pass", default=d["password"], help="Default: RABBITMQ_PASSWORD or guest")
    parser.add_argument(
        "--rabbit-vhost",
        default=d["vhost"],
        help="Default: RABBITMQ_VHOST or / (must match user permissions on the broker)",
    )
    parser.add_argument("--queue", default="job_scrape_requests")

    return parser.parse_args()


def read_raw_email_bytes(path: Optional[str]) -> bytes:
    if path:
        with open(path, "rb") as f:
            return f.read()
    return sys.stdin.buffer.read()


def parse_email(raw_bytes: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw_bytes)


def get_best_html_part(message) -> Optional[str]:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get_content_disposition() or "").lower()
            if content_type == "text/html" and disposition != "attachment":
                try:
                    return part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        if message.get_content_type() == "text/html":
            try:
                return message.get_content()
            except Exception:
                payload = message.get_payload(decode=True)
                charset = message.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    return None


def cleanup_href(url: str) -> str:
    url = url.strip()
    # Remove common garbage seen in pasted/raw HTML
    url = url.rstrip('"}\' \t\r\n')
    url = re.sub(r'[>\s]+$', '', url)

    parsed = urlparse(url)
    clean_query = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if any(k.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        clean_query.append((k, v))

    cleaned = parsed._replace(query=urlencode(clean_query, doseq=True))
    return urlunparse(cleaned)


def is_probable_job_link(url: str, text: str) -> bool:
    combined = f"{text} {url}".lower()

    if any(hint in combined for hint in IGNORE_TEXT_HINTS):
        return False

    if not url.startswith(("http://", "https://")):
        return False

    if any(host_hint in url.lower() for host_hint in JOB_HOST_HINTS):
        return True

    # Fallback heuristics
    positive_terms = ("job", "jobs", "career", "careers", "apply", "position", "opening")
    return any(term in combined for term in positive_terms)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def extract_summary_from_anchor(anchor) -> Optional[str]:
    texts = []
    node = anchor.parent
    # gather a modest local text window
    for _ in range(4):
        if node is None:
            break
        texts.append(clean_text(node.get_text(" ", strip=True)))
        node = node.next_sibling if hasattr(node, "next_sibling") else None

    merged = " ".join(t for t in texts if t)
    merged = clean_text(merged)

    # remove title if duplicated
    title = clean_text(anchor.get_text(" ", strip=True))
    if merged.startswith(title):
        merged = clean_text(merged[len(title):])

    return merged or None


def parse_job_card_line(anchor) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """
    Tries to parse nearby text like:
      1) Senior Staff Machine Learning Engineer ... (Remote - United States), Reddit - $266k to $372k
    """
    parent_text = clean_text(anchor.parent.get_text(" ", strip=True))
    title = clean_text(anchor.get_text(" ", strip=True))

    rank = None
    location = None
    company = None
    salary = None

    rank_match = re.search(r"(^|\s)(\d+)\)\s", parent_text)
    if rank_match:
        rank = int(rank_match.group(2))

    # remove leading rank + title
    working = parent_text
    working = re.sub(r"^\s*\d+\)\s*", "", working)
    if title:
        working = working.replace(title, "", 1).strip()

    # Parse `(location), company - salary`
    loc_match = re.match(r"^\((.*?)\)\s*,?\s*(.*)$", working)
    if loc_match:
        location = clean_text(loc_match.group(1))
        rest = clean_text(loc_match.group(2))
    else:
        rest = working

    if " - " in rest:
        left, right = rest.split(" - ", 1)
        company = clean_text(left) or None
        salary = clean_text(right) or None
    else:
        company = clean_text(rest) or None

    return rank, location, company, salary


def extract_jobs_from_html(html: str, subject: Optional[str], sender: Optional[str]) -> List[JobEntry]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: List[JobEntry] = []
    seen_urls = set()

    for anchor in soup.find_all("a", href=True):
        title = clean_text(anchor.get_text(" ", strip=True))
        raw_url = anchor.get("href", "")
        url = cleanup_href(raw_url)

        if not title:
            continue
        if not is_probable_job_link(url, title):
            continue

        rank, location, company, salary = parse_job_card_line(anchor)
        summary = extract_summary_from_anchor(anchor)

        if url in seen_urls:
            continue
        seen_urls.add(url)

        jobs.append(
            JobEntry(
                rank=rank,
                title=title,
                url=url,
                location=location,
                company=company,
                salary=salary,
                summary=summary,
                email_subject=subject,
                email_sender=sender,
            )
        )

    jobs.sort(key=lambda j: (j.rank is None, j.rank if j.rank is not None else 10**9, j.title.lower()))
    return jobs


def print_jobs(jobs: List[JobEntry]) -> None:
    if not jobs:
        print("No job links found.")
        return

    for job in jobs:
        print(f"[{job.rank if job.rank is not None else '?'}] {job.title}")
        if job.company or job.location:
            print(f"    company={job.company!r} location={job.location!r}")
        if job.salary:
            print(f"    salary={job.salary}")
        print(f"    url={job.url}")
        if job.summary:
            print(f"    summary={job.summary[:220]}")
        print()


def publish_jobs(
    jobs: List[JobEntry],
    host: str,
    port: int,
    user: str,
    password: str,
    vhost: str,
    queue_name: str,
) -> None:
    if pika is None:
        raise RuntimeError("pika is not installed. Run: pip install pika")

    credentials = pika.PlainCredentials(user, password)
    params = pika.ConnectionParameters(
        host=host,
        port=port,
        virtual_host=vhost,
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=30,
    )

    try:
        connection = pika.BlockingConnection(params)
    except pika.exceptions.ProbableAuthenticationError as e:
        print(
            "RabbitMQ refused the login (403 ACCESS_REFUSED). Check username, password, and vhost.\n"
            "  - vhost must be the one your user is granted on (e.g. 'jobs' not '/' if that is how the broker is set up).\n"
            "  - Match the same RABBITMQ_USER / RABBITMQ_PASSWORD / RABBITMQ_VHOST as the worker, or pass --rabbit-* flags.\n"
            f"  - Tried: user={user!r} vhost={vhost!r} host={host!r} port={port}",
            file=sys.stderr,
        )
        raise SystemExit(1) from e

    channel = connection.channel()

    channel.queue_declare(queue=queue_name, durable=True)

    for job in jobs:
        message = {
            "event_id": str(uuid.uuid4()),
            "event_type": "job.scrape.request",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": asdict(job),
        }

        channel.basic_publish(
            exchange="",
            routing_key=queue_name,
            body=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            ),
        )
        print(f"Published: {job.title}")

    connection.close()


def main() -> int:
    args = parse_args()

    if not args.dry_run and not args.publish:
        print("Specify at least one of --dry-run or --publish", file=sys.stderr)
        return 2

    raw_bytes = read_raw_email_bytes(args.raw_file)
    message = parse_email(raw_bytes)

    subject = message.get("Subject")
    sender = message.get("From")

    html = get_best_html_part(message)
    if not html:
        print("No HTML part found in the email.", file=sys.stderr)
        return 1

    jobs = extract_jobs_from_html(html, subject=subject, sender=sender)

    print_jobs(jobs)

    if args.publish:
        publish_jobs(
            jobs=jobs,
            host=args.rabbit_host,
            port=args.rabbit_port,
            user=args.rabbit_user,
            password=args.rabbit_pass,
            vhost=args.rabbit_vhost,
            queue_name=args.queue,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())