"""Concurrency regression tests for the state-machine services.

They call the service functions directly (no patching of ORM internals) from real threads
that are released together by a Barrier, and assert INVARIANTS that must hold whichever
thread wins. PostgreSQL only: SQLite serializes writers and ignores select_for_update, so a
pass there would prove nothing. Every worker thread closes its own DB connection.
"""
import threading
import unittest
from datetime import timedelta
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties import services
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus
from apps.bounties.services import InvalidTransition

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


def make_user(prefix):
    n = next(_seq)
    return User.objects.create(username=f"{prefix}{n}", email=f"{prefix}{n}@example.com")


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

    def test_new_claim_vs_accept_leaves_no_orphan_pending_claim(self):
        for _ in range(ITERATIONS):
            poster, bounty = self.fresh()
            claim1 = make_claim(bounty, self.c1)
            claimant2 = make_user("late")
            out = run_together(
                lambda: services.accept_claim(claim1.id, poster),
                lambda: services.create_claim(bounty.id, claimant2, "m" * 12, "c2"),
            )
            self.assertEqual(out[0][0], "ok", out)  # accepting claim1 always works
            bounty.refresh_from_db()
            self.assertEqual(bounty.status, BountyStatus.CLAIMED)
            # I3: no PENDING claim may exist on a non-OPEN bounty
            self.assertFalse(Claim.objects.filter(bounty=bounty, status=ClaimStatus.PENDING).exists())
            if out[1][0] == "err":
                self.assertIsInstance(out[1][1], InvalidTransition, out[1][1])

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
