"""Invariant I5 on the admin path: the admin cannot write state that belongs to the services."""
from datetime import timedelta

from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from .models import Bounty, BountyStatus, Claim, ClaimStatus


class AdminServiceOwnedStateTests(TestCase):
    def setUp(self):
        self.root = create_verified_user("root", "root@example.com", is_staff=True, is_superuser=True)
        self.poster = create_verified_user("poster", "poster@example.com")
        self.helper = create_verified_user("helper", "helper@example.com")
        self.bounty = Bounty.objects.create(
            poster=self.poster,
            title="Admin guard bounty",
            description="x" * 30,
            reward=100,
            deadline=timezone.now() + timedelta(days=2),
        )
        self.claim = Claim.objects.create(bounty=self.bounty, claimant=self.helper, message="m" * 12)
        self.client.force_login(self.root)

    def request(self):
        request = RequestFactory().post("/")
        request.user = self.root
        return request

    def form_for(self, obj, **changes):
        model_admin = admin.site._registry[type(obj)]
        request = self.request()
        form_class = model_admin.get_form(request, obj, change=True)
        initial = form_class(instance=obj)
        data = {}
        for name in initial.fields:
            value = initial[name].value()
            if name == "deadline":  # the admin splits datetimes into two inputs
                local = timezone.localtime(value)
                data["deadline_0"], data["deadline_1"] = local.date().isoformat(), local.time().strftime("%H:%M:%S")
            elif value not in (None, False):
                data[name] = value
        data.update(changes)
        return model_admin, request, form_class(data, instance=obj)

    def test_posted_state_is_ignored_on_a_bounty(self):
        model_admin, request, form = self.form_for(
            self.bounty, status=BountyStatus.CLAIMED, accepted_claim=self.claim.pk, title="Renamed by admin"
        )
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, change=True)
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.title, "Renamed by admin")
        self.assertEqual(self.bounty.status, BountyStatus.OPEN)
        self.assertIsNone(self.bounty.accepted_claim_id)

    def test_posted_state_is_ignored_on_a_claim(self):
        other = Bounty.objects.create(
            poster=self.poster,
            title="Another bounty",
            description="x" * 30,
            reward=100,
            deadline=timezone.now() + timedelta(days=2),
        )
        model_admin, request, form = self.form_for(
            self.claim, status=ClaimStatus.ACCEPTED, bounty=other.pk, claimant=self.root.pk, message="edited by admin"
        )
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, change=True)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.message, "edited by admin")
        self.assertEqual(self.claim.status, ClaimStatus.PENDING)
        self.assertEqual(self.claim.claimant, self.helper)
        self.assertEqual(self.claim.bounty, self.bounty)

    def test_save_does_not_undo_a_transition_that_committed_after_the_page_was_read(self):
        model_admin, request, form = self.form_for(self.bounty, title="Renamed by admin")
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)  # holds status=OPEN
        Bounty.objects.filter(pk=self.bounty.pk).update(status=BountyStatus.CANCELLED)  # the concurrent write
        model_admin.save_model(request, obj, form, change=True)
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CANCELLED)
        self.assertEqual(self.bounty.title, "Renamed by admin")

    def test_there_is_no_add_page(self):
        for name in ("bounty", "claim"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(f"admin:bounties_{name}_add")).status_code, 403)

    def test_change_pages_render(self):
        for obj, name in ((self.bounty, "bounty"), (self.claim, "claim")):
            with self.subTest(name):
                url = reverse(f"admin:bounties_{name}_change", args=[obj.pk])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'name="status"')
