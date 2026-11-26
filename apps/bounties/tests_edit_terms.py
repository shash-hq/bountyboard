"""After a claim exists the poster may only improve the offer (reward up, deadline later, scope frozen)."""
import unittest
from datetime import timedelta

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import services
from .exceptions import InvalidTransition
from .models import Bounty, Claim, ClaimStatus
from .tests_races import in_worker, wait_until_a_lock_request_is_waiting

PW = "pw-12345-abc"


def mkbounty(poster):
    return Bounty.objects.create(
        poster=poster, title="Original title", description="Original description " * 3, category="Tech",
        reward=1000, deadline=timezone.now() + timedelta(days=5),
    )


class EditTermsTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@gmail.com", PW)
        self.claimant = create_verified_user("claimant", "claimant@gmail.com", PW)
        self.bounty = mkbounty(self.poster)

    def claim(self, status=ClaimStatus.PENDING):
        c = Claim.objects.create(bounty=self.bounty, claimant=self.claimant, message="m" * 12)
        if status != ClaimStatus.PENDING:
            Claim.objects.filter(pk=c.pk).update(status=status)  # fixture only
        return c

    def edit(self, **fields):
        return services.update_bounty_details(self.bounty.pk, self.poster, **fields)

    def assert_refused(self, **fields):
        before = Bounty.objects.get(pk=self.bounty.pk)
        with self.assertRaises(InvalidTransition):
            self.edit(**fields)
        after = Bounty.objects.get(pk=self.bounty.pk)
        for f in ("title", "description", "category", "reward", "deadline"):
            self.assertEqual(getattr(after, f), getattr(before, f), f)

    def test_without_claims_everything_is_editable(self):
        b = self.edit(title="New title", reward=10, description="Brand new description " * 3, category="Design",
                      deadline=timezone.now() + timedelta(days=1))
        self.assertEqual((b.title, b.reward, b.category), ("New title", 10, "Design"))

    def test_with_a_claim_the_reward_cannot_go_down(self):
        self.claim()
        self.assert_refused(reward=10)

    def test_with_a_claim_the_scope_cannot_be_rewritten(self):
        self.claim()
        self.assert_refused(description="Totally different work " * 3)
        self.assert_refused(title="A different job")
        self.assert_refused(category="Design")

    def test_with_a_claim_the_deadline_cannot_be_shortened(self):
        self.claim()
        self.assert_refused(deadline=self.bounty.deadline - timedelta(days=2))

    def test_with_a_claim_improvements_are_allowed(self):
        self.claim()
        later = self.bounty.deadline + timedelta(days=2)
        b = self.edit(reward=1500, deadline=later)
        self.assertEqual((b.reward, b.deadline), (1500, later))

    def test_resubmitting_unchanged_terms_is_fine_even_with_the_deadline_cut_to_the_minute(self):
        self.claim()
        b = self.edit(
            title=self.bounty.title, description=self.bounty.description, category=self.bounty.category,
            reward=self.bounty.reward, deadline=self.bounty.deadline.replace(second=0, microsecond=0),
        )
        self.assertEqual(b.reward, 1000)

    def test_form_rounding_never_moves_the_stored_deadline_earlier(self):
        stored = self.bounty.deadline.replace(second=59, microsecond=0)
        Bounty.objects.filter(pk=self.bounty.pk).update(deadline=stored)
        self.claim()
        self.assertEqual(self.edit(deadline=stored.replace(second=0)).deadline, stored)
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.deadline, stored)

    def form_data(self, **extra):
        deadline = timezone.localtime(self.bounty.deadline).strftime("%Y-%m-%dT%H:%M")
        return {"reward": 1500, "deadline": deadline, **extra}

    def test_the_form_disables_scope_and_saves_without_it(self):
        self.client.force_login(self.poster)
        url = reverse("bounties:edit", args=[self.bounty.pk])
        fields = self.client.get(url).context["form"].fields
        self.assertFalse(any(fields[n].disabled for n in services.SCOPE_FIELDS))
        self.claim()
        fields = self.client.get(url).context["form"].fields
        self.assertTrue(all(fields[n].disabled for n in services.SCOPE_FIELDS))
        self.assertFalse(fields["reward"].disabled)
        self.client.post(url, self.form_data())  # a browser leaves disabled inputs out
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.reward, self.bounty.title), (1500, "Original title"))

    def test_a_stale_form_that_rewrites_scope_is_refused_not_silently_dropped(self):
        self.claim()
        self.client.force_login(self.poster)
        r = self.client.post(reverse("bounties:edit", args=[self.bounty.pk]), self.form_data(
            title="A different job", description=self.bounty.description, category="Tech"), follow=True)
        self.assertContains(r, "can&#x27;t change")
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.reward, self.bounty.title), (1000, "Original title"))

    def test_a_rejected_or_withdrawn_claim_still_counts(self):
        for st in (ClaimStatus.REJECTED, ClaimStatus.WITHDRAWN):
            Claim.objects.all().delete()
            self.claim(st)
            self.assert_refused(reward=10)

    def test_the_view_shows_the_refusal_and_the_form_explains_the_lock(self):
        self.claim()
        self.client.force_login(self.poster)
        url = reverse("bounties:edit", args=[self.bounty.pk])
        self.assertContains(self.client.get(url), "already has claims")
        r = self.client.post(url, {
            "title": self.bounty.title, "description": self.bounty.description, "category": "Tech",
            "reward": 10, "deadline": (self.bounty.deadline + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        }, follow=True)
        self.assertContains(r, "reward can only go up")
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.reward, 1000)


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class EditVersusClaimRaceTests(TransactionTestCase):
    """The claim check is made under the bounty lock. A claim committed while the edit waits for that
    lock must be seen by it; checking before the lock would let the cut through."""

    def test_a_claim_that_commits_first_blocks_the_edit(self):
        poster = create_verified_user("poster", "poster@gmail.com", PW)
        claimant = create_verified_user("claimant", "claimant@gmail.com", PW)
        bounty = mkbounty(poster)
        worker, outcome = in_worker(lambda: services.update_bounty_details(bounty.pk, poster, reward=10))
        with transaction.atomic():
            Bounty.objects.select_for_update().get(pk=bounty.pk)  # hold the bounty row
            worker.start()
            wait_until_a_lock_request_is_waiting()
            Claim.objects.create(bounty=bounty, claimant=claimant, message="m" * 12)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        self.assertIsInstance(outcome.get("err"), InvalidTransition, outcome)
        bounty.refresh_from_db()
        self.assertEqual(bounty.reward, 1000)
