# HOW TO RUN (this file lives in docs/audit/ on purpose, so CI does NOT discover it):
#   Django only discovers tests inside the importable apps, so copy it in first:
#     cp docs/audit/test_audit_repro.py apps/bounties/test_audit_repro.py
#     DATABASE_URL=postgres://localhost/bountyboard \
#       python manage.py test apps.bounties.test_audit_repro --settings=config.settings.dev
#   Needs a local PostgreSQL (the race tests are meaningless on SQLite: select_for_update is a no-op
#   and writers are serialized). Delete the copy afterwards; do not commit it under apps/.
#   Run it against UNMODIFIED code: every test asserts that a known bug EXISTS, so after a fix lands
#   the matching test is expected to fail.
"""Audit reproductions (22 Nov 2026).
Each test asserts the BUG exists. In Phase 2 these become regression tests with assertions flipped.
See the comment block above for how to run them."""
import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.forms import SignupForm
from apps.bounties import services
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus

User = get_user_model()


def in_thread(fn, *args):
    out = {}
    def target():
        try:
            out["r"] = fn(*args)
        except Exception as e:  # noqa
            out["e"] = e
        finally:
            connection.close()
    t = threading.Thread(target=target)
    t.start(); t.join()
    return out


def mk(poster, title="Audit reproduction bounty", **kw):
    return Bounty.objects.create(
        poster=poster, title=title, description="x" * 30, reward=100,
        deadline=timezone.now() + timedelta(days=2), **kw)


class RaceRepro(TransactionTestCase):
    def setUp(self):
        self.poster = User.objects.create_user("p", "p@x.in", "pw-Strong-123")
        self.c1 = User.objects.create_user("c1", "c1@x.in", "pw-Strong-123")
        self.c2 = User.objects.create_user("c2", "c2@x.in", "pw-Strong-123")
        self.b = mk(self.poster)
        self.cl1 = Claim.objects.create(bounty=self.b, claimant=self.c1, message="m" * 12, contact="c1")

    def test_withdraw_overwrites_accept(self):
        orig = Claim.save
        fired = []
        def hooked(inst, *a, **k):
            if inst.status == ClaimStatus.WITHDRAWN and not fired:
                fired.append(1)
                r = in_thread(services.accept_claim, inst.id, self.poster)
                assert "e" not in r, r
            return orig(inst, *a, **k)
        with mock.patch.object(Claim, "save", hooked):
            services.withdraw_claim(self.cl1.id, self.c1)
        self.b.refresh_from_db(); self.cl1.refresh_from_db()
        print(f"\n[withdraw-vs-accept] bounty={self.b.status} accepted_claim={self.b.accepted_claim_id} claim={self.cl1.status}")
        self.assertEqual(self.b.status, BountyStatus.CLAIMED)
        self.assertEqual(self.b.accepted_claim_id, self.cl1.id)
        self.assertEqual(self.cl1.status, ClaimStatus.WITHDRAWN)  # BUG: winner is 'withdrawn'

    def test_cancel_overwrites_accept(self):
        mgr = Bounty.objects
        orig_get = mgr.get
        fired = []
        def hooked_get(*a, **k):
            obj = orig_get(*a, **k)
            if not fired:
                fired.append(1)
                r = in_thread(services.accept_claim, self.cl1.id, self.poster)
                assert "e" not in r, r
            return obj
        with mock.patch.object(mgr, "get", hooked_get):
            services.cancel_bounty(self.b.id, self.poster)
        self.b.refresh_from_db(); self.cl1.refresh_from_db()
        print(f"\n[cancel-vs-accept] bounty={self.b.status} accepted_claim={self.b.accepted_claim_id} claim={self.cl1.status}")
        self.assertEqual(self.b.status, BountyStatus.CANCELLED)   # BUG: cancelled...
        self.assertEqual(self.cl1.status, ClaimStatus.ACCEPTED)   # ...with an accepted claim (poster email already revealed)

    def test_claim_lands_on_claimed_bounty(self):
        orig = Claim.full_clean
        fired = []
        def hooked(inst, *a, **k):
            orig(inst, *a, **k)
            if inst.pk is None and inst.claimant_id == self.c2.id and not fired:
                fired.append(1)
                r = in_thread(services.accept_claim, self.cl1.id, self.poster)
                assert "e" not in r, r
        self.client.force_login(self.c2)
        with mock.patch.object(Claim, "full_clean", hooked):
            self.client.post(reverse("bounties:claim", args=[self.b.pk]), {"message": "m" * 12, "contact": "c2"})
        self.b.refresh_from_db()
        orphan = Claim.objects.get(claimant=self.c2)
        print(f"\n[claim-vs-accept] bounty={self.b.status} new_claim={orphan.status}")
        self.assertEqual(self.b.status, BountyStatus.CLAIMED)
        self.assertEqual(orphan.status, ClaimStatus.PENDING)  # BUG: orphan pending claim, never resolved

    def test_open_cap_overflow(self):
        for i in range(3):
            mk(self.poster, title=f"Existing bounty number {i}")
        self.assertEqual(Bounty.objects.filter(poster=self.poster, status="OPEN").count(), 4)
        barrier = threading.Barrier(2, timeout=10)
        orig = Bounty.full_clean
        def hooked(inst, *a, **k):
            orig(inst, *a, **k)
            if inst.pk is None:
                barrier.wait()
        with mock.patch.object(Bounty, "full_clean", hooked):
            t = [threading.Thread(target=in_thread, args=(mk, self.poster, f"Concurrent create {i}")) for i in range(2)]
            [x.start() for x in t]; [x.join() for x in t]
        n = Bounty.objects.filter(poster=self.poster, status="OPEN").count()
        print(f"\n[5-cap] open bounties after 2 concurrent creates = {n}")
        self.assertEqual(n, 6)  # BUG: invariant 'max 5 OPEN' violated

    def test_double_submit_claim_500(self):
        barrier = threading.Barrier(2, timeout=10)
        orig = Claim.full_clean
        def hooked(inst, *a, **k):
            orig(inst, *a, **k)
            if inst.pk is None:
                barrier.wait()
        res = []
        def sub():
            res.append(in_thread(lambda: Claim(bounty=self.b, claimant=self.c2, message="m" * 12, contact="x").save()))
        with mock.patch.object(Claim, "full_clean", hooked):
            t = [threading.Thread(target=sub) for _ in range(2)]
            [x.start() for x in t]; [x.join() for x in t]
        errs = [type(r["e"]).__name__ for r in res if "e" in r]
        print(f"\n[double-submit] errors={errs}")
        self.assertIn("IntegrityError", errs)  # view only catches ValidationError -> HTTP 500


class LogicRepro(TestCase):
    @override_settings(ALLOWED_EMAIL_DOMAIN="iitb.ac.in")
    def test_domain_suffix_bypass(self):
        f = SignupForm(data={"username": "eve", "email": "eve@notiitb.ac.in",
                             "password": "Zx9!long-pass", "password_confirm": "Zx9!long-pass"})
        print(f"\n[domain] notiitb.ac.in accepted for iitb.ac.in: {f.is_valid()}")
        self.assertTrue(f.is_valid())

    def test_validation_error_repr_in_flash(self):
        p = User.objects.create_user("p2", "p2@x.in", "pw-Strong-123")
        b = mk(p); b.status = BountyStatus.DONE; b.save()
        self.client.force_login(p)
        r = self.client.post(reverse("bounties:cancel", args=[b.pk]), follow=True)
        msgs = [str(m) for m in r.context["messages"]]
        print(f"\n[flash] {msgs}")
        self.assertTrue(any(m.startswith("['") for m in msgs))

    def test_deadline_sort_surfaces_expired_first(self):
        p = User.objects.create_user("p3", "p3@x.in", "pw-Strong-123")
        live = mk(p, title="Live bounty due soon")
        dead = mk(p, title="Expired bounty from earlier")
        Bounty.objects.filter(pk=dead.pk).update(deadline=timezone.now() - timedelta(days=30))
        r = self.client.get("/?sort=deadline_soon&status=OPEN")
        first = list(r.context["bounties"])[0]
        print(f"\n[board] first result for 'deadline_soon' + OPEN tab: {first.title!r} expired={first.is_expired}")
        self.assertTrue(first.is_expired)

    def test_anonymous_detail_view_creates_session_row(self):
        from django.contrib.sessions.models import Session
        p = User.objects.create_user("p4", "p4@x.in", "pw-Strong-123")
        b = mk(p)
        before = Session.objects.count()
        for _ in range(3):
            self.client.cookies.clear()
            self.client.get(b.get_absolute_url())
        print(f"\n[sessions] 3 anonymous crawler hits created {Session.objects.count() - before} session rows")
        self.assertEqual(Session.objects.count() - before, 3)

    def test_edit_after_claims_bait_and_switch(self):
        p = User.objects.create_user("p5", "p5@x.in", "pw-Strong-123")
        c = User.objects.create_user("c5", "c5@x.in", "pw-Strong-123")
        b = mk(p); b.reward = 2000; b.save()
        Claim.objects.create(bounty=b, claimant=c, message="m" * 12, contact="c5")
        self.client.force_login(p)
        self.client.post(reverse("bounties:edit", args=[b.pk]), {
            "title": b.title, "description": "totally different scope now " * 2, "category": "Tech",
            "reward": 10, "deadline": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")})
        b.refresh_from_db()
        print(f"\n[edit] reward after pending claim exists: {b.reward}")
        self.assertEqual(b.reward, 10)
