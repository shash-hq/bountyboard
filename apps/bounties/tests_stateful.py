"""Random sequences of service calls, with the invariants checked after every step.

Actors are chosen at random too, so permission and transition refusals are exercised; a refusal
(DomainError) is a normal outcome and must leave the invariants intact. Single-threaded: concurrency
is covered by the race tests. I7 (the outbox) has no table yet, so it is not checked here.
"""
from datetime import timedelta

from django.conf import settings as django_settings
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule, run_state_machine_as_test

from apps.accounts import campus
from apps.accounts.testing import campus_verify, create_verified_user
from apps.bounties import services
from apps.bounties.exceptions import DomainError
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus
from apps.bounties.policies import can_view_contact, is_campus_verified, open_bounty_cap

ALLOWED_BOUNTY_MOVES = {
    (BountyStatus.OPEN, BountyStatus.CLAIMED),
    (BountyStatus.OPEN, BountyStatus.CANCELLED),
    (BountyStatus.CLAIMED, BountyStatus.DONE),
}
ALLOWED_CLAIM_MOVES = {
    (ClaimStatus.PENDING, ClaimStatus.ACCEPTED),
    (ClaimStatus.PENDING, ClaimStatus.REJECTED),
    (ClaimStatus.PENDING, ClaimStatus.WITHDRAWN),
}

people = st.integers(0, 3)
picks = st.integers(0, 10**6)  # index into whatever rows exist at that step
usually_the_owner = st.sampled_from([True, True, True, False])


class Lifecycle(RuleBasedStateMachine):
    moved: set = set()  # moves the rules made across all examples, so the test cannot pass by doing nothing

    def __init__(self):
        super().__init__()
        self.atomic = transaction.atomic()
        self.atomic.__enter__()
        # Users 0-2 are campus-verified, user 3 is only email-verified.
        self.users = [create_verified_user(f"u{i}", f"u{i}@example.org", campus=i < 3) for i in range(4)]
        self.bounty_status: dict = {}
        self.claim_status: dict = {}

    @initialize()
    def seed_a_claimed_and_an_open_bounty(self):
        """Random sequences rarely get from nothing to an acceptance, so every run starts with one
        bounty already CLAIMED and one OPEN with two pending claims. What is seeded does not count
        towards the coverage guard: only moves the rules make afterwards do."""
        for poster in (0, 1):
            bounty = services.create_bounty(
                self.users[poster],
                title="Seeded bounty",
                description="x" * 30,
                reward=100,
                deadline=timezone.now() + timedelta(days=2),
            )
            for who in {0, 1, 2} - {poster}:
                services.create_claim(bounty.pk, self.users[who], "m" * 12)
            if poster == 0:
                services.accept_claim(bounty.claims.order_by("pk").first().pk, bounty.poster)
        self.bounty_status = dict(Bounty.objects.values_list("pk", "status"))
        self.claim_status = dict(Claim.objects.values_list("pk", "status"))

    def teardown(self):
        transaction.set_rollback(True)
        self.atomic.__exit__(None, None, None)

    def attempt(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError:
            return None

    def pick(self, model, index, prefer=None):
        """A row for a rule to act on. Mostly one in the ``prefer`` status, so that successful
        transitions are common; every third pick is any row, so illegal ones are still tried."""
        rows = list(model.objects.order_by("pk"))
        if prefer and index % 3:
            rows = [r for r in rows if r.status == prefer] or rows
        return rows[index % len(rows)] if rows else None

    @rule(who=people)
    def post(self, who):
        user = self.users[who]
        created = self.attempt(
            services.create_bounty,
            user,
            title="Stateful test bounty",
            description="x" * 30,
            reward=100,
            deadline=timezone.now() + timedelta(days=2),
        )
        if created:
            # I4 by tier, checked when posting: a lapsed user may legitimately keep more than the lower cap.
            assert services.count_live_open_bounties(user) <= open_bounty_cap(user)

    @rule(which=picks, who=people)
    def pitch(self, which, who):
        if bounty := self.pick(Bounty, which, BountyStatus.OPEN):
            self.attempt(services.create_claim, bounty.pk, self.users[who], "m" * 12)

    @rule(which=picks, owner=usually_the_owner, who=people)
    def accept(self, which, owner, who):
        if claim := self.pick(Claim, which, ClaimStatus.PENDING):
            actor = claim.bounty.poster if owner else self.users[who]
            if self.attempt(services.accept_claim, claim.pk, actor):
                # I9: both parties verified at the moment of acceptance
                assert is_campus_verified(claim.bounty.poster) and is_campus_verified(claim.claimant)

    @rule(which=picks, owner=usually_the_owner, who=people)
    def reject(self, which, owner, who):
        if claim := self.pick(Claim, which, ClaimStatus.PENDING):
            self.attempt(services.reject_claim, claim.pk, claim.bounty.poster if owner else self.users[who])

    @rule(which=picks, owner=usually_the_owner, who=people)
    def withdraw(self, which, owner, who):
        if claim := self.pick(Claim, which, ClaimStatus.PENDING):
            self.attempt(services.withdraw_claim, claim.pk, claim.claimant if owner else self.users[who])

    @rule(which=picks, owner=usually_the_owner, who=people)
    def cancel(self, which, owner, who):
        if bounty := self.pick(Bounty, which, BountyStatus.OPEN):
            self.attempt(services.cancel_bounty, bounty.pk, bounty.poster if owner else self.users[who])

    @rule(which=picks, owner=usually_the_owner, who=people)
    def done(self, which, owner, who):
        if bounty := self.pick(Bounty, which, BountyStatus.CLAIMED):
            self.attempt(services.mark_bounty_done, bounty.pk, bounty.poster if owner else self.users[who])

    @rule(who=people)
    def lapse_or_renew_verification(self, who):
        user = self.users[who]
        if is_campus_verified(user):
            campus.supersede_active(user, timezone.now())
        else:
            campus_verify(user)

    @invariant()
    def i1_at_most_one_accepted_claim(self):
        accepted = list(Claim.objects.filter(status=ClaimStatus.ACCEPTED).values_list("bounty_id", flat=True))
        assert len(accepted) == len(set(accepted))

    @invariant()
    def i2_i3_bounty_and_claims_agree(self):
        for bounty in Bounty.objects.select_related("accepted_claim"):
            held = bounty.status in (BountyStatus.CLAIMED, BountyStatus.DONE)
            claim = bounty.accepted_claim
            assert held == (claim is not None)
            if claim is not None:
                assert claim.status == ClaimStatus.ACCEPTED and claim.bounty_id == bounty.pk
            else:
                assert not bounty.claims.filter(status=ClaimStatus.ACCEPTED).exists()
            if bounty.status != BountyStatus.OPEN:
                assert not bounty.claims.filter(status=ClaimStatus.PENDING).exists()

    @invariant()
    def i4_open_cap(self):
        for user in self.users:
            assert services.count_live_open_bounties(user) <= django_settings.OPEN_BOUNTY_CAP_CAMPUS_VERIFIED

    @invariant()
    def i5_only_allowed_moves(self):
        for model, seen, allowed, start in (
            (Bounty, self.bounty_status, ALLOWED_BOUNTY_MOVES, BountyStatus.OPEN),
            (Claim, self.claim_status, ALLOWED_CLAIM_MOVES, ClaimStatus.PENDING),
        ):
            for pk, status in model.objects.values_list("pk", "status"):
                before = seen.get(pk, start)
                assert before == status or (before, status) in allowed, (model.__name__, before, status)
                if before != status:
                    self.moved.add((model.__name__, before, status))
                seen[pk] = status

    @invariant()
    def i6_contact_only_between_the_accepted_pair(self):
        for bounty in Bounty.objects.select_related("accepted_claim"):
            claim = bounty.accepted_claim
            pair = {bounty.poster_id, claim.claimant_id} if claim else set()
            for viewer in self.users:
                for subject in self.users:
                    expected = viewer.pk != subject.pk and {viewer.pk, subject.pk} == pair
                    assert can_view_contact(viewer, subject, bounty) == expected

    @invariant()
    def i8_one_claim_per_person_and_no_self_claim(self):
        pairs = list(Claim.objects.values_list("bounty_id", "claimant_id", "bounty__poster_id"))
        assert len({(b, c) for b, c, _ in pairs}) == len(pairs)
        assert all(claimant != poster for _, claimant, poster in pairs)


class LifecycleInvariantTests(TestCase):
    def test_invariants_hold_over_random_sequences(self):
        Lifecycle.moved = set()
        run_state_machine_as_test(
            Lifecycle,
            settings=settings(
                max_examples=100,
                stateful_step_count=30,
                deadline=None,
                derandomize=True,  # same sequences every run, so CI cannot flake
                database=None,
                suppress_health_check=[HealthCheck.too_slow],
            ),
        )
        expected = {("Bounty", *move) for move in ALLOWED_BOUNTY_MOVES}
        expected |= {("Claim", *move) for move in ALLOWED_CLAIM_MOVES}
        self.assertEqual(expected - Lifecycle.moved, set(), "some transitions were never exercised")
