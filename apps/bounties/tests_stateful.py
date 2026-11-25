"""Random sequences of service calls, with the invariants checked after every step.

Actors are chosen at random too, so permission and transition refusals are exercised; a refusal
(DomainError) is a normal outcome and must leave the invariants intact. Single-threaded: concurrency
is covered by the race tests. I7 is checked as: each move a rule makes leaves exactly one
notification row for it.
"""
from datetime import timedelta

from django.conf import settings as django_settings
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule, run_state_machine_as_test

from apps.accounts import campus, moderation
from apps.accounts.testing import campus_verify, create_verified_user
from apps.bounties import services
from apps.bounties.exceptions import DomainError
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus, Notification
from apps.bounties.models import NotificationKind as K
from apps.bounties.policies import can_view_contact, is_campus_verified, open_bounty_cap

ALLOWED_BOUNTY_MOVES = {
    (BountyStatus.OPEN, BountyStatus.CLAIMED),
    (BountyStatus.OPEN, BountyStatus.CANCELLED),
    (BountyStatus.OPEN, BountyStatus.EXPIRED),
    (BountyStatus.CLAIMED, BountyStatus.DONE),
    (BountyStatus.CLAIMED, BountyStatus.CANCELLED),  # only after the counterparty is revoked
}
ALLOWED_CLAIM_MOVES = {
    (ClaimStatus.PENDING, ClaimStatus.ACCEPTED),
    (ClaimStatus.PENDING, ClaimStatus.REJECTED),
    (ClaimStatus.PENDING, ClaimStatus.WITHDRAWN),
    (ClaimStatus.ACCEPTED, ClaimStatus.CANCELLED),  # only with the move above
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
        self.moderator = create_verified_user("mod", "mod@example.org", is_superuser=True)
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

    @rule(which=picks)
    def expire(self, which):
        if bounty := self.pick(Bounty, which, BountyStatus.OPEN):
            # A clock past every deadline in this test, instead of editing the row.
            self.attempt(services.expire_bounty, bounty.pk, timezone.now() + timedelta(days=3))

    @rule()
    def renew_lapsed_verifications(self):
        """Lapses and revocations pile up; without this, long runs have nobody left who can accept."""
        for user in self.users[:3]:
            if not is_campus_verified(user):
                campus_verify(user)

    @rule(which=picks, of_poster=st.booleans(), reason=st.sampled_from(moderation.MODERATOR_REASONS))
    def revoke_a_party(self, which, of_poster, reason):
        """Aimed at parties of claimed bounties: revoking at random starves acceptance entirely."""
        claimed = list(Bounty.objects.filter(status=BountyStatus.CLAIMED).order_by("pk"))
        if claimed:
            bounty = claimed[which % len(claimed)]
            party = bounty.poster if of_poster else bounty.accepted_claim.claimant
            self.attempt(moderation.revoke_verification, self.moderator.pk, party.pk, reason)

    @rule(which=picks, of_poster=st.booleans(), reason=st.sampled_from(moderation.MODERATOR_REASONS))
    def revoke_then_counterparty_cancels(self, which, of_poster, reason):
        """The two steps in order. Left to chance they rarely line up, and the cancel path goes untested."""
        claimed = list(Bounty.objects.filter(status=BountyStatus.CLAIMED).order_by("pk"))
        if claimed:
            bounty = claimed[which % len(claimed)]
            parties = [bounty.poster, bounty.accepted_claim.claimant]
            revoked, other = parties if of_poster else parties[::-1]
            self.attempt(moderation.revoke_verification, self.moderator.pk, revoked.pk, reason)
            self.attempt(services.cancel_after_counterparty_revocation, bounty.pk, other)

    @rule(which=picks, by_poster=st.booleans())
    def cancel_after_revocation(self, which, by_poster):
        if bounty := self.pick(Bounty, which, BountyStatus.CLAIMED):
            claim = bounty.accepted_claim
            actor = bounty.poster if by_poster or claim is None else claim.claimant
            self.attempt(services.cancel_after_counterparty_revocation, bounty.pk, actor)

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
                    self.assert_one_notification(model.__name__, pk, status)
                seen[pk] = status

    def assert_one_notification(self, model, pk, status):
        """I7: the row that reports this move exists, once. A cancel or expiry of an OPEN bounty
        reports through its claims' rows, and the poster's own cancel notifies nobody else."""
        if model == "Claim":
            claim = Claim.objects.get(pk=pk)
            bounty_id, poster, claimant = claim.bounty_id, claim.bounty.poster_id, claim.claimant_id
            recipients, kinds = {
                ClaimStatus.ACCEPTED: ({claimant}, {K.CLAIM_ACCEPTED}),
                ClaimStatus.WITHDRAWN: ({poster}, {K.CLAIM_WITHDRAWN}),
                ClaimStatus.REJECTED: ({claimant}, {K.CLAIM_REJECTED, K.BOUNTY_CANCELLED, K.BOUNTY_EXPIRED}),
                ClaimStatus.CANCELLED: ({poster, claimant}, {K.COUNTERPARTY_CANCELLED}),  # whoever didn't cancel
            }[status]
            keys = {
                ClaimStatus.ACCEPTED: [f"claim-accepted:{pk}"],
                ClaimStatus.WITHDRAWN: [f"claim-withdrawn:{pk}"],
                ClaimStatus.REJECTED: [
                    f"claim-rejected:{pk}", f"bounty-cancelled:{bounty_id}:{pk}", f"bounty-expired:{bounty_id}:{pk}",
                ],
                ClaimStatus.CANCELLED: [f"cancel-after-revocation:{bounty_id}:{pk}"],
            }[status]
        elif status == BountyStatus.DONE:
            keys = [f"bounty-done:{pk}"]
            recipients, kinds = {Bounty.objects.get(pk=pk).accepted_claim.claimant_id}, {K.BOUNTY_DONE}
        elif status == BountyStatus.EXPIRED:
            keys = [f"bounty-expired:{pk}"]
            recipients, kinds = {Bounty.objects.get(pk=pk).poster_id}, {K.BOUNTY_EXPIRED}
        else:
            return
        found = list(Notification.objects.filter(idempotency_key__in=keys).values_list("recipient_id", "kind"))
        assert len(found) == 1 and found[0][0] in recipients and found[0][1] in kinds, (model, pk, status, found)

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
