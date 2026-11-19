"""Deterministic synthetic data for the report models (January to June 2026).

Everything here is invented: people, teams, projects and numbers. The generators
build the same inputs the real pipelines receive (connector ``WorkItem`` objects,
security posture snapshots, help desk tickets) and run them through the model
converters, so demos and offline tests exercise the whole path.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from nexdash.connectors.models import WorkItem
from nexdash.executor import DatabaseExecutor, SQLiteExecutor
from nexdash.report_models.dev_productivity import dev_rows_from_work_items
from nexdash.report_models.security_posture import SEVERITIES, security_rows_from_snapshots
from nexdash.report_models.support_metrics import support_rows_from_tickets

DEFAULT_SEED = 11
START = dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC)
DAYS = 181  # January to June 2026

TEAM_MEMBERS: dict[str, tuple[str, ...]] = {
    "platform": ("alice", "bruno", "carla"),
    "apps": ("diego", "elena", "farid"),
    "data": ("gabi", "hiro"),
}
TEAM_OF: dict[str, str] = {person: team for team, people in TEAM_MEMBERS.items() for person in people}
DEV_PROJECTS = ("example/api", "example/web")

SECURITY_PROJECTS: dict[str, str] = {
    "example/api": "platform",
    "example/web": "apps",
    "example/mobile": "apps",
    "example/pipeline": "data",
}

SUPPORT_PRODUCTS: dict[str, str] = {"billing-app": "tier1", "mobile-app": "tier1", "api": "tier2"}
SUPPORT_CHANNELS = ("email", "chat", "phone", "web")


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - synthetic demo data, not security sensitive


def generate_work_items(seed: int = DEFAULT_SEED) -> tuple[list[WorkItem], dict[str, float], list[str]]:
    """Return (work items, review hours by item key, merged pull request keys)."""
    rng = _rng(seed)
    people = list(TEAM_OF)
    end = START + dt.timedelta(days=DAYS)
    items: list[WorkItem] = []
    reviews: dict[str, float] = {}
    merged_keys: list[str] = []
    review_base = {"platform": 6.0, "apps": 14.0, "data": 9.0}

    for index in range(1, 261):
        person = rng.choice(people)
        project = rng.choice(DEV_PROJECTS)
        created = START + dt.timedelta(hours=rng.randrange(DAYS * 24))
        key = f"{project.split('/')[1]}#{index}"
        review = round(review_base[TEAM_OF[person]] * rng.uniform(0.3, 2.0), 1)
        # Lead time drops over the half-year as review gets faster.
        month_factor = 1.0 - 0.08 * (created.month - 1)
        lead = round(review + rng.uniform(4.0, 60.0) * month_factor, 1)
        closed_at = created + dt.timedelta(hours=lead)
        outcome = rng.choices(("merged", "closed", "open"), weights=(78, 8, 14))[0]
        if closed_at > end:
            outcome = "open"
        state = "open" if outcome == "open" else "closed"
        items.append(
            WorkItem(
                source="github",
                project=project,
                item_id=str(1000 + index),
                item_key=key,
                title=f"Change {index}",
                item_type="pull_request",
                status=outcome,
                state=state,
                author=person,
                assignee=person,
                created_at=created,
                closed_at=None if state == "open" else closed_at,
            )
        )
        reviews[key] = review
        if outcome == "merged":
            merged_keys.append(key)

    for index in range(1, 141):
        person = rng.choice(people)
        project = rng.choice(DEV_PROJECTS)
        created = START + dt.timedelta(hours=rng.randrange(DAYS * 24))
        closed_at = created + dt.timedelta(hours=rng.uniform(12.0, 400.0))
        closed = rng.random() < 0.75 and closed_at <= end
        items.append(
            WorkItem(
                source="github",
                project=project,
                item_id=str(5000 + index),
                item_key=f"{project.split('/')[1]}#i{index}",
                title=f"Issue {index}",
                item_type="issue",
                status="completed" if closed else "open",
                state="closed" if closed else "open",
                author=rng.choice(people),
                assignee=person,
                created_at=created,
                closed_at=closed_at if closed else None,
            )
        )
    return items, reviews, merged_keys


def generate_dev_rows(seed: int = DEFAULT_SEED) -> list[dict[str, Any]]:
    items, reviews, merged_keys = generate_work_items(seed)
    return dev_rows_from_work_items(items, team_of=TEAM_OF, review_hours=reviews, merged_keys=merged_keys)


def generate_security_snapshots(seed: int = DEFAULT_SEED) -> list[dict[str, Any]]:
    """Monthly posture snapshots for four projects, shaped like ``SecurityPostureMetrics``."""
    rng = _rng(seed)
    snapshots: list[dict[str, Any]] = []
    for project in SECURITY_PROJECTS:
        base = {"critical": rng.randint(2, 5), "high": rng.randint(5, 10), "medium": rng.randint(8, 16)}
        for month in range(1, 7):
            decay = 1.0 - 0.15 * (month - 1)
            counts = {
                "critical": max(0, round(base["critical"] * decay - rng.random())),
                "high": max(0, round(base["high"] * decay + rng.uniform(-1, 1))),
                "medium": max(0, round(base["medium"] * (0.6 + 0.4 * decay) + rng.uniform(-2, 2))),
                "low": rng.randint(3, 9),
                "info": rng.randint(0, 4),
            }
            outdated = [
                {"package_name": f"lib-{n}", "current_version": "1.0.0", "recommended_version": "1.2.0",
                 "severity": rng.choice(SEVERITIES[:4])}
                for n in range(rng.randint(1, 7 - month // 2))
            ]
            coverage = min(100.0, 55.0 + 8.0 * month + rng.uniform(-4, 4))
            snapshots.append(
                {
                    "project": project,
                    "period": f"2026-{month:02d}",
                    "severity_counts": counts,
                    "outdated_dependencies": outdated,
                    "outdated_dependencies_count": len(outdated),
                    "gate_coverage_pct": round(coverage, 1),
                    "gate_passed": counts["critical"] == 0 and counts["high"] <= 4,
                }
            )
    return snapshots


def generate_security_rows(seed: int = DEFAULT_SEED) -> list[dict[str, Any]]:
    return security_rows_from_snapshots(generate_security_snapshots(seed), team_of=SECURITY_PROJECTS)


def generate_tickets(seed: int = DEFAULT_SEED, count: int = 320) -> list[dict[str, Any]]:
    """Help desk tickets shaped like the Support Engine ``Ticket`` model."""
    rng = _rng(seed)
    end = START + dt.timedelta(days=DAYS)
    response_base = {"email": 240.0, "chat": 8.0, "phone": 3.0, "web": 180.0}
    tickets: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        created = START + dt.timedelta(minutes=rng.randrange(DAYS * 24 * 60))
        channel = rng.choice(SUPPORT_CHANNELS)
        product = rng.choice(list(SUPPORT_PRODUCTS))
        deflected = channel in ("chat", "web") and rng.random() < 0.3
        status = rng.choices(("solved", "closed", "open", "pending"), weights=(55, 20, 15, 10))[0]
        resolved = status in ("solved", "closed")
        comments = [{"id": f"c{index}-1", "author_role": "customer", "body": "Question", "created_at": created}]
        if not deflected:
            month_factor = 1.0 - 0.07 * (created.month - 1)
            reply = created + dt.timedelta(minutes=response_base[channel] * rng.uniform(0.4, 1.6) * month_factor)
            if reply <= end:
                comments.append({"id": f"c{index}-2", "author_role": "agent", "body": "Answer", "created_at": reply})
        else:
            comments.append({"id": f"c{index}-2", "author_role": "system", "body": "Article", "created_at": created})
        solved_at = created + dt.timedelta(hours=rng.uniform(0.5, 72.0)) if resolved else None
        score = round(rng.uniform(-1.0, 1.0) * 0.8 + (0.2 if resolved else -0.2), 2)
        tickets.append(
            {
                "id": f"T{index:05d}",
                "source": "helpdesk",
                "subject": f"Ticket {index}",
                "status": status,
                "priority": rng.choice(("low", "normal", "high", "urgent")),
                "product": product,
                "tags": ["deflected"] if deflected else [],
                "created_at": created,
                "solved_at": solved_at,
                "csat": round(rng.choice((0.0, 0.25, 0.5, 0.75, 1.0, 1.0)), 2) if resolved else None,
                "comments": comments,
                "extra": {"channel": channel, "sentiment_score": max(-1.0, min(score, 1.0))},
            }
        )
    return tickets


def generate_support_rows(seed: int = DEFAULT_SEED) -> list[dict[str, Any]]:
    return support_rows_from_tickets(generate_tickets(seed), team_of=SUPPORT_PRODUCTS)


def generate_model_rows(seed: int = DEFAULT_SEED) -> dict[str, list[dict[str, Any]]]:
    """Synthetic rows for every model, keyed by model name."""
    return {
        "dev_productivity": generate_dev_rows(seed),
        "security_posture": generate_security_rows(seed),
        "support_metrics": generate_support_rows(seed),
    }


def seed_model_data(executor: DatabaseExecutor | SQLiteExecutor, seed: int = DEFAULT_SEED) -> dict[str, int]:
    """Create every model table (dropping earlier copies) and load synthetic rows. Returns rows per table."""
    from nexdash.report_models import REPORT_MODELS

    counts: dict[str, int] = {}
    for name, rows in generate_model_rows(seed).items():
        model = REPORT_MODELS[name]
        counts[model.table.name] = model.load_rows(executor, rows)
    return counts
