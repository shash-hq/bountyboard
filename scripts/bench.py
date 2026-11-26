"""Time the paths that matter on a small instance, and count their queries.

    DATABASE_URL=postgres://localhost/bountyboard make bench

Creates a throwaway test database (never touches the one DATABASE_URL names), fills it, and drops it.

These are numbers from the machine it runs on. The production instance has 0.1 CPU, so password
hashing and template rendering will be many times slower there: measure those on the instance. What
carries over is the query count, and whether it grows with the data.
"""
import argparse
import os
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# dev, not test: test settings swap in a fast password hasher, and hashing is one thing measured here.
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.dev"

import django  # noqa: E402

django.setup()

from django.contrib.auth.hashers import check_password, get_hasher, make_password  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import (  # noqa: E402
    CaptureQueriesContext,
    setup_databases,
    setup_test_environment,
    teardown_databases,
    teardown_test_environment,
)
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.testing import create_verified_user  # noqa: E402
from apps.bounties import services  # noqa: E402
from apps.bounties.models import Bounty, BountyStatus, Category, Claim  # noqa: E402

CLAIM_COUNTS = (2, 20)  # two detail pages: if their query counts differ, the page has an N+1


def measure(fn, repeat):
    """(median ms, p95 ms, queries) over ``repeat`` calls."""
    times, queries = [], 0
    for _ in range(repeat):
        with CaptureQueriesContext(connection) as ctx:
            start = time.perf_counter()
            fn()
            times.append((time.perf_counter() - start) * 1000)
        queries = max(queries, len(ctx.captured_queries))
    times.sort()
    return statistics.median(times), times[max(0, int(len(times) * 0.95) - 1)], queries


def row(name, result):
    median, p95, queries = result
    print(f"{name:<44} {median:>9.1f} {p95:>9.1f} {queries:>8}")


def get(client, url):
    def call():
        status = client.get(url).status_code
        assert status == 200, f"{url} returned {status}"

    return call


def make_bounties(users, count):
    now = timezone.now()
    categories = Category.values
    Bounty.objects.bulk_create(
        Bounty(
            poster=users[i % len(users)],
            title=f"Benchmark task number {i}",
            slug=f"benchmark-task-number-{i}",
            description="Solder a header and check continuity on the board. " * 3,
            category=categories[i % len(categories)],
            reward=10 + (i % 500) * 10,
            # a quarter already overdue, as on a board whose sweeper is behind
            deadline=now + timedelta(hours=(i % 400) - 100),
        )
        for i in range(count)
    )


def make_open_bounty(poster):
    return Bounty.objects.create(
        poster=poster, title="Benchmark accept target", description="x" * 40, reward=100,
        deadline=timezone.now() + timedelta(days=3),
    )


def run(bounties, repeat):
    users = [create_verified_user(f"bench{i}", f"bench{i}@example.org", campus=True) for i in range(max(CLAIM_COUNTS) + 1)]
    poster, claimants = users[0], users[1:]
    make_bounties(users, bounties)

    print(f"PostgreSQL {connection.pg_version}, {bounties} bounties, {repeat} runs each")
    print(f"{'path':<44} {'median ms':>9} {'p95 ms':>9} {'queries':>8}")

    hasher = get_hasher()
    encoded = make_password("benchmark-password")
    row(f"check_password ({hasher.algorithm}, {hasher.iterations} iterations)",
        measure(lambda: check_password("benchmark-password", encoded), repeat))

    client = Client()
    client.force_login(claimants[0])
    board = reverse("board")
    for label, query in (
        ("board, newest", ""),
        ("board, deadline soon", "?sort=deadline_soon"),
        ("board, open tab", "?status=OPEN"),
        ("board, search", "?q=number+42"),
        ("board, last page", f"?page={max(1, bounties // 12)}"),
    ):
        row(label, measure(get(client, board + query), repeat))

    poster_client = Client()
    poster_client.force_login(poster)
    for count in CLAIM_COUNTS:
        bounty = make_open_bounty(poster)
        for claimant in claimants[:count]:
            Claim.objects.create(bounty=bounty, claimant=claimant, message="I can do this today.")
        url = reverse("bounties:detail", args=[bounty.pk, bounty.slug])
        row(f"detail as poster, {count} claims", measure(get(poster_client, url), repeat))
    row("my page", measure(get(poster_client, reverse("me")), repeat))

    pending = []
    for _ in range(repeat):
        bounty = make_open_bounty(poster)
        pending.append(Claim.objects.create(bounty=bounty, claimant=claimants[0], message="I can do this today."))
    row("accept_claim, with its on-commit email", measure(lambda: services.accept_claim(pending.pop().pk, poster), repeat))
    overdue = Bounty.objects.filter(status=BountyStatus.OPEN, deadline__lte=timezone.now()).count()
    swept = []
    result = measure(lambda: swept.append(services.sweep_expired()), 1)
    row(f"sweep_expired, one pass closing {swept[0]} of {overdue} overdue", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bounties", type=int, default=1000, help="rows on the board (default 1000)")
    parser.add_argument("--repeat", type=int, default=20, help="runs per path (default 20)")
    args = parser.parse_args()
    if args.bounties < 1 or args.repeat < 1:
        parser.error("--bounties and --repeat must be at least 1")
    if connection.vendor != "postgresql":
        sys.exit("DATABASE_URL must point at PostgreSQL: SQLite timings say nothing about production.")

    setup_test_environment()
    databases = setup_databases(verbosity=0, interactive=False)
    try:
        run(args.bounties, args.repeat)
    finally:
        teardown_databases(databases, verbosity=0)
        teardown_test_environment()


if __name__ == "__main__":
    main()
