"""Concurrency regression tests for the state-machine services.

They call the service functions directly (no patching of ORM internals) from real threads
that are released together by a Barrier, and assert INVARIANTS that must hold whichever
thread wins. PostgreSQL only: SQLite serializes writers and ignores select_for_update, so a
pass there would prove nothing. Every worker thread closes its own DB connection.
"""
import threading
import time
import unittest
from datetime import timedelta
from itertools import count

from django.contrib.auth import get_user_model
from apps.accounts.testing import create_verified_user
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties import services
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus
from apps.bounties.exceptions import InvalidTransition, LimitReached, NotPermitted

User = get_user_model()
ITERATIONS = 15  # a race is probabilistic per attempt; repeat to make a regression likely to show
_seq = count()


def run_together(*calls):
    """Run each zero-arg callable in its own thread, released at the same instant.
    Returns a list of ("ok", value) / ("err", exception) in the order given."""
    barrier = threading.Barrier(len(calls), timeout=15)
    out = [None] * len(calls)

    def worker(i, fn):
        try:
            barrier.wait()
            out[i] = ("ok", fn())
        except Exception as e:  # noqa: BLE001 - the outcome IS the assertion subject
            out[i] = ("err", e)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "worker thread hung (deadlock?)"
    return out


def wait_until_a_lock_request_is_waiting(timeout=10.0):
    """Block until some backend is waiting on a lock (the worker reached the row we hold)."""
    deadline = time.monotonic() + timeout
    with connection.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
            if cur.fetchone()[0]:
                return
            time.sleep(0.01)
    raise AssertionError("worker never blocked on the lock we hold")


def in_worker(fn):
    """Start fn in a thread (own DB connection, closed in finally). Returns (thread, outcome)."""
    outcome = {}

    def target():
        try:
            outcome["ok"] = fn()
        except Exception as e:  # noqa: BLE001 - the outcome IS the assertion subject
            outcome["err"] = e
        finally:
            connections.close_all()

    t = threading.Thread(target=target)
    return t, outcome


def make_user(prefix):
    n = next(_seq)
    return create_verified_user(username=f"{prefix}{n}", email=f"{prefix}{n}@example.com", campus=True)


def make_bounty(poster):
    return Bounty.objects.create(
        poster=poster,
        title="Race regression bounty",
        description="x" * 30,
        reward=100,
        deadline=timezone.now() + timedelta(days=2),
    )


def make_claim(bounty, claimant):
    return Claim.objects.create(bounty=bounty, claimant=claimant, message="m" * 12, contact="c")


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class TransitionRaceTests(TransactionTestCase):
    def setUp(self):
        self.poster = make_user("poster")
        self.c1 = make_user("c1_")
        self.c2 = make_user("c2_")

    def fresh(self):
        poster = make_user("poster")
        bounty = make_bounty(poster)
        return poster, bounty

    def assert_one_winner(self, outcomes):
        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(len(errs), 1, outcomes)
        self.assertIsInstance(errs[0][1], InvalidTransition, errs[0][1])

    def test_accept_vs_withdraw_same_claim(self):
        for _ in range(ITERATIONS):
            poster, bounty = self.fresh()
            claim = make_claim(bounty, self.c1)
            out = run_together(
                lambda: services.accept_claim(claim.id, poster),
                lambda: services.withdraw_claim(claim.id, self.c1),
            )
            self.assert_one_winner(out)
            bounty.refresh_from_db()
            claim.refresh_from_db()
            if out[0][0] == "ok":  # accept won: the accepted claim must stay ACCEPTED
                self.assertEqual((bounty.status, bounty.accepted_claim_id), (BountyStatus.CLAIMED, claim.id))
                self.assertEqual(claim.status, ClaimStatus.ACCEPTED)
            else:  # withdraw won: nothing may be accepted
                self.assertEqual((bounty.status, bounty.accepted_claim_id), (BountyStatus.OPEN, None))
                self.assertEqual(claim.status, ClaimStatus.WITHDRAWN)

    def test_accept_vs_cancel(self):
        for _ in range(ITERATIONS):
            poster, bounty = self.fresh()
            claim = make_claim(bounty, self.c1)
            out = run_together(
                lambda: services.accept_claim(claim.id, poster),
                lambda: services.cancel_bounty(bounty.id, poster),
            )
            self.assert_one_winner(out)
            bounty.refresh_from_db()
            claim.refresh_from_db()
            if out[0][0] == "ok":  # accept won: bounty must not end up CANCELLED
                self.assertEqual((bounty.status, claim.status), (BountyStatus.CLAIMED, ClaimStatus.ACCEPTED))
                self.assertEqual(bounty.accepted_claim_id, claim.id)
            else:  # cancel won: no accepted claim, pending claim rejected
                self.assertEqual((bounty.status, claim.status), (BountyStatus.CANCELLED, ClaimStatus.REJECTED))
                self.assertIsNone(bounty.accepted_claim_id)

    def test_claim_blocked_by_bounty_lock_cannot_land_on_accepted_bounty(self):
        """Deterministic claim-vs-accept, no timing luck.

        The main thread holds the bounty row lock (inside atomic) and accepts claim1 while a
        second thread tries to create a claim. The second thread must WAIT for the lock, then
        re-read the bounty (now CLAIMED) and refuse. Without select_for_update it would validate
        against the stale OPEN row and insert an orphan PENDING claim once the lock is released.
        """
        poster, bounty = self.fresh()
        claim1 = make_claim(bounty, self.c1)
        late = make_user("late")
        outcome = {}

        def late_claim():
            try:
                outcome["ok"] = services.create_claim(bounty.id, late, "m" * 12, "c2")
            except Exception as e:  # noqa: BLE001
                outcome["err"] = e
            finally:
                connections.close_all()

        worker = threading.Thread(target=late_claim)
        with transaction.atomic():
            Bounty.objects.select_for_update().get(id=bounty.id)  # hold the row lock
            worker.start()
            wait_until_a_lock_request_is_waiting()
            services.accept_claim(claim1.id, poster)  # state changes while the worker waits
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung (deadlock?)")

        self.assertIn("err", outcome, outcome)
        self.assertIsInstance(outcome["err"], InvalidTransition, outcome["err"])
        bounty.refresh_from_db()
        self.assertEqual(bounty.status, BountyStatus.CLAIMED)
        # I3: no PENDING claim may exist on a non-OPEN bounty
        self.assertFalse(Claim.objects.filter(bounty=bounty, status=ClaimStatus.PENDING).exists())
        self.assertFalse(Claim.objects.filter(bounty=bounty, claimant=late).exists())

    def test_double_submit_claim_is_idempotent_not_a_crash(self):
        for _ in range(ITERATIONS):
            _, bounty = self.fresh()
            claimant = make_user("dbl")
            out = run_together(
                lambda: services.create_claim(bounty.id, claimant, "m" * 12, "c"),
                lambda: services.create_claim(bounty.id, claimant, "m" * 12, "c"),
            )
            self.assert_one_winner(out)  # no IntegrityError / 500
            self.assertEqual(Claim.objects.filter(bounty=bounty, claimant=claimant).count(), 1)

    def test_accept_vs_accept_different_claims(self):
        for _ in range(ITERATIONS):
            poster, bounty = self.fresh()
            claim1 = make_claim(bounty, self.c1)
            claim2 = make_claim(bounty, self.c2)
            out = run_together(
                lambda: services.accept_claim(claim1.id, poster),
                lambda: services.accept_claim(claim2.id, poster),
            )
            self.assert_one_winner(out)
            self.assertEqual(Claim.objects.filter(bounty=bounty, status=ClaimStatus.ACCEPTED).count(), 1)

    def test_mark_done_vs_cancel_on_claimed_bounty(self):
        """cancel vs mark_done on a CLAIMED bounty: cancel must refuse, state stays consistent."""
        poster, bounty = self.fresh()
        claim = make_claim(bounty, self.c1)
        services.accept_claim(claim.id, poster)
        out = run_together(
            lambda: services.mark_bounty_done(bounty.id, poster),
            lambda: services.cancel_bounty(bounty.id, poster),
        )
        self.assert_one_winner(out)
        bounty.refresh_from_db()
        self.assertEqual(bounty.status, BountyStatus.DONE)


class FriendlyErrorViewTests(TestCase):
    """A lost race or stale page must produce a flash message, not a 500 or "['...']"."""

    def setUp(self):
        self.poster = make_user("vposter")
        self.c1 = make_user("vc1_")
        self.c2 = make_user("vc2_")
        self.bounty = make_bounty(self.poster)
        self.claim1 = make_claim(self.bounty, self.c1)
        self.claim2 = make_claim(self.bounty, self.c2)

    def flashes(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_second_accept_shows_friendly_message(self):
        self.client.force_login(self.poster)
        self.client.post(reverse("claim_accept", args=[self.claim1.pk]))
        r = self.client.post(reverse("claim_accept", args=[self.claim2.pk]), follow=True)
        msgs = self.flashes(r)
        self.assertTrue(msgs and not any(m.startswith("[") for m in msgs), msgs)

    def test_cancel_after_accept_shows_friendly_message_not_500(self):
        self.client.force_login(self.poster)
        self.client.post(reverse("claim_accept", args=[self.claim1.pk]))
        r = self.client.post(reverse("bounties:cancel", args=[self.bounty.pk]), follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Only OPEN bounties can be cancelled.", self.flashes(r))
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CLAIMED)

    def test_duplicate_claim_post_is_a_flash_not_a_500(self):
        self.client.force_login(self.c1)
        r = self.client.post(
            reverse("bounties:claim", args=[self.bounty.pk]), {"message": "m" * 12, "contact": "c"}, follow=True
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Claim.objects.filter(bounty=self.bounty, claimant=self.c1).count(), 1)


BOUNTY_FIELDS = dict(
    title="Cap race regression bounty",
    description="x" * 30,
    category="Tech",
    reward=100,
)


def bounty_fields():
    return dict(BOUNTY_FIELDS, deadline=timezone.now() + timedelta(days=2))


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class CreateBountyCapRaceTests(TransactionTestCase):
    """I4: at most 5 OPEN bounties per user, even under concurrent creates."""

    def poster_with_open(self, n):
        poster = make_user("capposter")  # cap 5 needs a campus verification
        for _ in range(n):
            make_bounty(poster)
        return poster

    def open_count(self, poster):
        return Bounty.objects.filter(poster=poster, status=BountyStatus.OPEN).count()

    def test_concurrent_creates_at_four_open_yield_exactly_five(self):
        for _ in range(ITERATIONS):
            poster = self.poster_with_open(4)
            out = run_together(
                lambda: services.create_bounty(poster, **bounty_fields()),
                lambda: services.create_bounty(poster, **bounty_fields()),
            )
            self.assertEqual([o[0] for o in sorted(out)], ["err", "ok"], out)
            self.assertIsInstance(
                next(o[1] for o in out if o[0] == "err"), LimitReached)
            self.assertEqual(self.open_count(poster), 5)

    def test_create_blocked_by_user_lock_cannot_exceed_cap(self):
        """Deterministic: main holds the poster's USER row lock and creates the 5th bounty
        while a second create waits. When the lock is released the waiter must re-count (5)
        and refuse. Without the user lock it counts a stale 4 and inserts a 6th."""
        poster = self.poster_with_open(4)
        worker, outcome = in_worker(lambda: services.create_bounty(poster, **bounty_fields()))
        with transaction.atomic():
            User.objects.select_for_update().get(pk=poster.pk)  # hold the user row lock
            worker.start()
            wait_until_a_lock_request_is_waiting()
            make_bounty(poster)  # the 5th, committed when the block exits
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung (deadlock?)")
        self.assertIn("err", outcome, outcome)
        self.assertIsInstance(outcome["err"], LimitReached)
        self.assertEqual(self.open_count(poster), 5)


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class EditBountyRaceTests(TransactionTestCase):
    def setUp(self):
        self.poster = make_user("editposter")
        self.claimant = make_user("editclaimant")
        self.bounty = make_bounty(self.poster)
        self.claim = make_claim(self.bounty, self.claimant)

    def test_edit_blocked_by_bounty_lock_cannot_edit_an_accepted_bounty(self):
        """Deterministic: main holds the bounty lock and accepts the claim while the poster's
        edit waits. The edit must then re-read the bounty (CLAIMED) and refuse, leaving the
        accepted state and the reward untouched. Without the lock the edit validates against
        the stale OPEN row and rewrites the reward after acceptance."""
        worker, outcome = in_worker(
            lambda: services.update_bounty_details(self.bounty.id, self.poster, reward=10))
        with transaction.atomic():
            Bounty.objects.select_for_update().get(id=self.bounty.id)
            worker.start()
            wait_until_a_lock_request_is_waiting()
            services.accept_claim(self.claim.id, self.poster)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung (deadlock?)")

        self.assertIn("err", outcome, outcome)
        self.assertIsInstance(outcome["err"], InvalidTransition)
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CLAIMED)
        self.assertEqual(self.bounty.accepted_claim_id, self.claim.id)
        self.assertEqual(self.bounty.reward, 100)

    def test_edit_vs_cancel_never_resurrects_the_bounty(self):
        for _ in range(ITERATIONS):
            bounty = make_bounty(make_user("racer"))
            out = run_together(
                lambda: services.update_bounty_details(bounty.id, bounty.poster, reward=500),
                lambda: services.cancel_bounty(bounty.id, bounty.poster),
            )
            bounty.refresh_from_db()
            self.assertEqual(bounty.status, BountyStatus.CANCELLED, out)  # a stale save must not reopen it


class BountyEditServiceTests(TestCase):
    def setUp(self):
        self.poster = make_user("svcposter")
        self.other = make_user("svcother")
        self.bounty = make_bounty(self.poster)

    def test_edit_updates_only_editable_fields(self):
        services.update_bounty_details(self.bounty.id, self.poster, title="A brand new title", reward=250)
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.title, self.bounty.reward), ("A brand new title", 250))
        self.assertEqual(self.bounty.status, BountyStatus.OPEN)

    def test_non_poster_cannot_edit(self):
        with self.assertRaises(NotPermitted):
            services.update_bounty_details(self.bounty.id, self.other, reward=250)

    def test_cannot_edit_after_cancel(self):
        services.cancel_bounty(self.bounty.id, self.poster)
        with self.assertRaises(InvalidTransition):
            services.update_bounty_details(self.bounty.id, self.poster, reward=250)

    def test_status_is_not_an_editable_field(self):
        with self.assertRaises(TypeError):
            services.update_bounty_details(self.bounty.id, self.poster, status=BountyStatus.DONE)

    def test_view_refuses_edit_when_bounty_stopped_being_open(self):
        self.client.force_login(self.poster)
        services.cancel_bounty(self.bounty.id, self.poster)
        r = self.client.post(
            reverse("bounties:edit", args=[self.bounty.pk]),
            dict(BOUNTY_FIELDS, reward=300, deadline=(timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")),
            follow=True,
        )
        self.assertEqual(r.status_code, 200)
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.status, self.bounty.reward), (BountyStatus.CANCELLED, 100))

    def test_create_bounty_rejects_sixth_open(self):
        for _ in range(3):  # setUp already made one: 4 open, so exactly one more is allowed
            make_bounty(self.poster)
        services.create_bounty(self.poster, **bounty_fields())
        with self.assertRaises(LimitReached):
            services.create_bounty(self.poster, **bounty_fields())
