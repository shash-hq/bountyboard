import threading
from datetime import timedelta
from django.contrib.auth import get_user_model
from apps.accounts.testing import create_verified_user
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, OperationalError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties.exceptions import InvalidTransition, NotPermitted
from apps.bounties.models import (
    Bounty,
    BountyStatus,
    Category,
    Claim,
    ClaimStatus,
    Report,
    ReportReason,
)
from apps.bounties.services import (
    accept_claim,
    cancel_bounty,
    hide_bounty,
    mark_bounty_done,
    reject_claim,
    resolve_report,
    withdraw_claim,
)
from apps.bounties.templatetags.bounty_tags import inr_format, time_left

User = get_user_model()


class BountyModelAndTagTests(TestCase):
    def setUp(self):
        self.user = create_verified_user(
            username="arjun", email="arjun@campus.edu.in", password="Password123!"
        )

    def test_inr_format_filter(self):
        """Verify inr_format correctly formats rupee amounts with Indian grouping."""
        self.assertEqual(inr_format(100), "₹100")
        self.assertEqual(inr_format(1500), "₹1,500")
        self.assertEqual(inr_format(10000), "₹10,000")
        self.assertEqual(inr_format(100000), "₹1,00,000")

    def test_time_left_filter(self):
        """Verify time_left returns accurate humanized countdowns."""
        now = timezone.now()
        past = now - timedelta(hours=1)
        self.assertEqual(time_left(past), "Expired")

        future_days = now + timedelta(days=3, hours=2)
        self.assertIn("days left", time_left(future_days))

        future_hours = now + timedelta(hours=2, minutes=30)
        self.assertIn("2h", time_left(future_hours))

    def test_bounty_clean_validations(self):
        """Verify title, description, reward and future deadline constraints."""
        future = timezone.now() + timedelta(days=2)

        # Title too short (< 8)
        b_short_title = Bounty(
            poster=self.user,
            title="Short",
            description="Valid long description exceeding twenty characters.",
            reward=100,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_short_title.save()

        # Description too short (< 20)
        b_short_desc = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Too short",
            reward=100,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_short_desc.save()

        # Reward too low (< 10)
        b_low_reward = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Valid long description exceeding twenty characters.",
            reward=5,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_low_reward.save()

        # Reward too high (> 10000)
        b_high_reward = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Valid long description exceeding twenty characters.",
            reward=15000,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_high_reward.save()

        # Past deadline for new bounty
        b_past_deadline = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Valid long description exceeding twenty characters.",
            reward=100,
            deadline=timezone.now() - timedelta(hours=2),
        )
        with self.assertRaises(ValidationError):
            b_past_deadline.save()


class BountyViewAndFilterTests(TestCase):
    def setUp(self):
        self.user1 = create_verified_user(
            username="poster1", email="poster1@campus.edu.in", password="Password123!"
        )
        self.user2 = create_verified_user(
            username="student2", email="student2@campus.edu.in", password="Password123!"
        )
        future = timezone.now() + timedelta(days=3)

        self.b1 = Bounty.objects.create(
            poster=self.user1,
            title="Arduino Sensor Debugging Task",
            description="Fix I2C temperature sensor read errors in robotics laboratory.",
            category=Category.TECH,
            reward=300,
            deadline=future,
            status=BountyStatus.OPEN,
        )
        self.b2 = Bounty.objects.create(
            poster=self.user1,
            title="Poster Design for Cultural Fest",
            description="Create vector graphics and A3 layout for annual music festival.",
            category=Category.DESIGN,
            reward=800,
            deadline=future + timedelta(days=1),
            status=BountyStatus.OPEN,
        )

    def test_board_view_search_and_filters(self):
        """Verify ?q=, ?category=, and ?status= filtering."""
        self.client.force_login(self.user2)  # the board needs a login
        res_search = self.client.get(reverse("board"), {"q": "Arduino"})
        self.assertEqual(res_search.status_code, 200)
        self.assertContains(res_search, "Arduino Sensor Debugging Task")
        self.assertNotContains(res_search, "Poster Design for Cultural Fest")

        res_cat = self.client.get(reverse("board"), {"category": "Design"})
        self.assertEqual(res_cat.status_code, 200)
        self.assertContains(res_cat, "Poster Design for Cultural Fest")
        self.assertNotContains(res_cat, "Arduino Sensor Debugging Task")

    def test_bounty_create_view_enforces_auth(self):
        """Verify unauthenticated user is redirected to login."""
        res = self.client.get(reverse("bounties:create"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("accounts/login", res.url)

    def test_bounty_create_view_successful(self):
        """Verify authenticated user can post a bounty."""
        self.client.force_login(self.user2)
        deadline = (timezone.now() + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")
        res = self.client.post(
            reverse("bounties:create"),
            {
                "title": "Verilog ALU Simulation Help",
                "description": "Need an 8-bit arithmetic logic unit simulated with full testbench waveforms.",
                "category": "Tech",
                "reward": 450,
                "deadline": deadline,
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Bounty.objects.filter(title="Verilog ALU Simulation Help").exists())

    def test_bounty_edit_permissions(self):
        """Verify only the poster can edit while OPEN."""
        # Non-poster tries to edit -> 403 Forbidden
        self.client.force_login(self.user2)
        res_forbidden = self.client.get(reverse("bounties:edit", kwargs={"pk": self.b1.pk}))
        self.assertEqual(res_forbidden.status_code, 403)

        # Poster edits -> 200 OK
        self.client.force_login(self.user1)
        res_ok = self.client.get(reverse("bounties:edit", kwargs={"pk": self.b1.pk}))
        self.assertEqual(res_ok.status_code, 200)

    def test_seed_demo_command(self):
        """Verify seed_demo runs cleanly without error."""
        call_command("seed_demo", allow_non_debug=True)  # the test runner forces DEBUG=False
        self.assertGreaterEqual(Bounty.objects.count(), 20)
        self.assertTrue(User.objects.filter(username="arjun_ece").exists())


class ClaimsLifecycleAndPermissionDenialTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(
            username="poster", email="poster@campus.edu.in", password="Password123!"
        )
        self.claimant1 = create_verified_user(
            username="claimant1", email="claimant1@campus.edu.in", password="Password123!"
        )
        self.claimant2 = create_verified_user(
            username="claimant2", email="claimant2@campus.edu.in", password="Password123!"
        )
        self.bounty = Bounty.objects.create(
            poster=self.poster,
            title="Robotics Motor Driver Fix",
            description="H-bridge PWM driver overheating during test runs.",
            category=Category.TECH,
            reward=500,
            deadline=timezone.now() + timedelta(days=3),
            status=BountyStatus.OPEN,
        )

    def test_cannot_claim_own_bounty(self):
        """Rule: Poster cannot claim their own bounty."""
        claim = Claim(
            bounty=self.bounty,
            claimant=self.poster,
            message="Trying to claim my own task.",
            contact="WhatsApp 123",
        )
        with self.assertRaises(ValidationError):
            claim.save()

        # Also via view
        self.client.force_login(self.poster)
        res = self.client.post(
            reverse("bounties:claim", kwargs={"pk": self.bounty.pk}),
            {"message": "I will solve my own bounty", "contact": "+91 9999999999"},
            follow=True,
        )
        self.assertEqual(self.bounty.claims.count(), 0)

    def test_cannot_claim_closed_or_expired_bounty(self):
        """Rule: Cannot claim cancelled, done, or expired bounty."""
        self.bounty.status = BountyStatus.CANCELLED
        self.bounty.save()

        claim = Claim(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="Valid pitch for a cancelled bounty.",
            contact="WhatsApp 123",
        )
        with self.assertRaises(ValidationError):
            claim.save()

    def test_cannot_claim_twice(self):
        """Rule: Same user cannot claim the same bounty twice."""
        Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First valid proposal for this bounty.",
            contact="WhatsApp 123",
            status=ClaimStatus.PENDING,
        )
        duplicate = Claim(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="Second proposal for the same task.",
            contact="WhatsApp 123",
            status=ClaimStatus.PENDING,
        )
        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_atomic_accept_and_reject_others(self):
        """
        Rule: Accept runs atomically; chosen becomes ACCEPTED, others become REJECTED,
        bounty becomes CLAIMED, and emails are dispatched.
        """
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
        )
        c2 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant2,
            message="Second proposal from claimant 2.",
            contact="WhatsApp: +91 9222222222",
        )

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            accepted = accept_claim(c1.id, self.poster)

        c1.refresh_from_db()
        c2.refresh_from_db()
        self.bounty.refresh_from_db()

        self.assertEqual(c1.status, ClaimStatus.ACCEPTED)
        self.assertEqual(c2.status, ClaimStatus.REJECTED)
        self.assertEqual(self.bounty.status, BountyStatus.CLAIMED)
        self.assertEqual(self.bounty.accepted_claim, c1)

        # Check notification emails were sent
        self.assertGreaterEqual(len(mail.outbox), 1)

    def test_non_poster_cannot_accept_claim(self):
        """Rule: Only the poster can accept claims."""
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
        )
        with self.assertRaises(NotPermitted):
            accept_claim(c1.id, self.claimant2)

    def test_non_poster_cannot_reject_claim(self):
        """Rule: Only the poster can reject claims."""
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
        )
        with self.assertRaises(NotPermitted):
            reject_claim(c1.id, self.claimant2)

    def test_claimant_can_withdraw_pending_claim(self):
        """Rule: Claimant can withdraw their pending claim."""
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
        )
        withdrawn = withdraw_claim(c1.id, self.claimant1)
        self.assertEqual(withdrawn.status, ClaimStatus.WITHDRAWN)

    def test_non_claimant_cannot_withdraw_claim(self):
        """Rule: Only the claimant can withdraw their claim."""
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
        )
        with self.assertRaises(NotPermitted):
            withdraw_claim(c1.id, self.claimant2)

    def test_cannot_withdraw_non_pending_claim(self):
        """Rule: Cannot withdraw accepted or rejected claims."""
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
            status=ClaimStatus.ACCEPTED,
        )
        with self.assertRaises(InvalidTransition):
            withdraw_claim(c1.id, self.claimant1)

    def test_mark_done_rules(self):
        """Rule: Only poster can mark done, and only when CLAIMED."""
        # When OPEN, cannot mark done
        with self.assertRaises(InvalidTransition):
            mark_bounty_done(self.bounty.id, self.poster)

        # Transition to CLAIMED
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="First proposal from claimant 1.",
            contact="WhatsApp: +91 9111111111",
        )
        accept_claim(c1.id, self.poster)

        # Non-poster cannot mark done
        with self.assertRaises(NotPermitted):
            mark_bounty_done(self.bounty.id, self.claimant1)

        # Poster marks done -> SUCCESS
        done_bounty = mark_bounty_done(self.bounty.id, self.poster)
        self.assertEqual(done_bounty.status, BountyStatus.DONE)

    def test_cancel_rules(self):
        """Rule: Only poster can cancel, and only when OPEN."""
        # Non-poster cannot cancel
        with self.assertRaises(NotPermitted):
            cancel_bounty(self.bounty.id, self.claimant1)

        # Poster cancels -> SUCCESS
        cancelled = cancel_bounty(self.bounty.id, self.poster)
        self.assertEqual(cancelled.status, BountyStatus.CANCELLED)

        # Once CANCELLED, cannot cancel again
        with self.assertRaises(InvalidTransition):
            cancel_bounty(self.bounty.id, self.poster)

    def test_contact_visibility_rules(self):
        """
        Rule: Claim contact visible ONLY to poster.
        After accept, claimant sees poster's email.
        """
        c1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.claimant1,
            message="Pitch with secret contact info.",
            contact="+91 9999988888",
        )

        # Anonymous / other student view: does NOT see contact
        self.client.force_login(self.claimant2)
        res_other = self.client.get(
            reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})
        )
        self.assertNotContains(res_other, "+91 9999988888")

        # Poster view BEFORE accept: does NOT see the claimant's contact (decision 3)
        self.client.force_login(self.poster)
        res_poster = self.client.get(
            reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})
        )
        self.assertNotContains(res_poster, "+91 9999988888")

        # Before accept: claimant does NOT see poster's email in status block
        self.client.force_login(self.claimant1)
        res_claimant_before = self.client.get(
            reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})
        )
        self.assertNotContains(res_claimant_before, "POSTER EMAIL:")

        # Accept claim
        accept_claim(c1.id, self.poster)

        # After accept: claimant DOES see poster's email
        res_claimant_after = self.client.get(
            reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})
        )
        self.assertContains(res_claimant_after, "POSTER EMAIL:")
        self.assertContains(res_claimant_after, self.poster.email)

        # After accept: the poster now sees the accepted claimant's contact
        self.client.force_login(self.poster)
        res_poster_after = self.client.get(
            reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})
        )
        self.assertContains(res_poster_after, "+91 9999988888")


class ConcurrencyDoubleAcceptRaceTests(TransactionTestCase):
    """
    Tests for race condition prevention during claim acceptance using select_for_update.
    """
    def setUp(self):
        self.poster = create_verified_user(
            username="race_poster", email="race_poster@campus.edu.in", password="Password123!"
        )
        self.c1_user = create_verified_user(
            username="race_c1", email="race_c1@campus.edu.in", password="Password123!"
        )
        self.c2_user = create_verified_user(
            username="race_c2", email="race_c2@campus.edu.in", password="Password123!"
        )
        self.bounty = Bounty.objects.create(
            poster=self.poster,
            title="Concurrency Race Test Bounty",
            description="Testing concurrent accept calls on the same bounty.",
            category=Category.TECH,
            reward=400,
            deadline=timezone.now() + timedelta(days=2),
            status=BountyStatus.OPEN,
        )
        self.claim1 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.c1_user,
            message="Proposal 1 for race condition check.",
            contact="contact1",
        )
        self.claim2 = Claim.objects.create(
            bounty=self.bounty,
            claimant=self.c2_user,
            message="Proposal 2 for race condition check.",
            contact="contact2",
        )

    def test_double_accept_race_prevented(self):
        """
        Verify that even if two requests attempt to accept claim1 and claim2 simultaneously,
        only one succeeds and the other is safely rejected with ValidationError without corrupting state.
        """
        results = []
        errors = []

        def try_accept(claim_id):
            import time
            try:
                for attempt in range(5):
                    try:
                        c = accept_claim(claim_id, self.poster)
                        results.append(c.id)
                        return
                    except (OperationalError, DatabaseError) as e:
                        if "locked" in str(e).lower() and attempt < 4:
                            time.sleep(0.05)
                            continue
                        errors.append(e)
                        return
                    except Exception as e:
                        errors.append(e)
                        return
            finally:
                # Each worker thread opens its own DB connection. Close it when the thread
                # ends, otherwise it stays open and teardown cannot drop the test database
                # ("database is being accessed by other users").
                connections.close_all()

        t1 = threading.Thread(target=try_accept, args=(self.claim1.id,))
        t2 = threading.Thread(target=try_accept, args=(self.claim2.id,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly ONE thread must succeed
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertTrue(isinstance(errors[0], (InvalidTransition, ValidationError, DatabaseError, OperationalError)))

        # Verify DB state: Bounty is CLAIMED, accepted_claim points to winner, other is REJECTED
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CLAIMED)
        self.assertIn(self.bounty.accepted_claim.id, [self.claim1.id, self.claim2.id])

        accepted_count = Claim.objects.filter(bounty=self.bounty, status=ClaimStatus.ACCEPTED).count()
        rejected_count = Claim.objects.filter(bounty=self.bounty, status=ClaimStatus.REJECTED).count()
        self.assertEqual(accepted_count, 1)
        self.assertEqual(rejected_count, 1)
class SessionAndCookieFeatureTests(TestCase):
    def setUp(self):
        self.user = create_verified_user(
            username="session_tester", email="session@campus.edu.in", password="Password123!"
        )
        self.client.force_login(self.user)  # board and detail pages need a login
        future = timezone.now() + timedelta(days=5)
        self.bounties = []
        for i in range(7):
            poster = create_verified_user(
                username=f"poster_user_{i+1}",
                email=f"poster{i+1}@campus.edu.in",
                password="Password123!",
            )
            b = Bounty.objects.create(
                poster=poster,
                title=f"Session Testing Bounty #{i+1}",
                description="Valid long description for testing session tracking in detail views.",
                category=Category.TECH,
                reward=100 + i * 25,
                deadline=future,
                status=BountyStatus.OPEN,
            )
            self.bounties.append(b)

    def test_recently_viewed_session_tracking(self):
        """
        Verify visiting bounties updates session['recently_viewed_bounties'],
        maintains max 5 entries, and orders by most recently viewed (MRU).
        """
        # Visit first 6 bounties in order: 0, 1, 2, 3, 4, 5
        for b in self.bounties[:6]:
            self.client.get(reverse("bounties:detail", kwargs={"pk": b.pk, "slug": b.slug}))

        session = self.client.session
        recent = session.get("recently_viewed_bounties", [])
        
        # Max 5 entries
        self.assertEqual(len(recent), 5)
        
        # Most recent first: b5, b4, b3, b2, b1 (b0 should have been pushed out)
        expected_ids = [self.bounties[i].id for i in [5, 4, 3, 2, 1]]
        self.assertEqual(recent, expected_ids)

        # Re-visiting b2 brings it to the front
        self.client.get(reverse("bounties:detail", kwargs={"pk": self.bounties[2].pk, "slug": self.bounties[2].slug}))
        session = self.client.session
        recent_after = session.get("recently_viewed_bounties", [])
        self.assertEqual(recent_after[0], self.bounties[2].id)
        self.assertEqual(len(recent_after), 5)

    def test_recently_viewed_strip_rendered_on_board(self):
        """Verify the board renders the recent strip when session contains viewed bounties."""
        b = self.bounties[0]
        self.client.get(reverse("bounties:detail", kwargs={"pk": b.pk, "slug": b.slug}))

        res_board = self.client.get(reverse("board"))
        self.assertEqual(res_board.status_code, 200)
        self.assertContains(res_board, "RECENT:")
        self.assertContains(res_board, b.title[:20])

    def test_layout_cookie_switch_and_rendering(self):
        """Verify bb_layout cookie switches board between grid and list modes."""
        # Set to list
        resp = self.client.post(reverse("pages:set_layout"), {"layout": "list"}, HTTP_REFERER="/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.cookies["bb_layout"].value, "list")

        # Load board with cookie set to list
        self.client.cookies["bb_layout"] = "list"
        res_list = self.client.get(reverse("board"))
        self.assertContains(res_list, 'class="bounty-list"')

        # Load board with cookie set to grid
        self.client.cookies["bb_layout"] = "grid"
        res_grid = self.client.get(reverse("board"))
        self.assertContains(res_grid, 'class="bounty-grid"')


class ModeratorWorkflowTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(
            username="poster_student", email="poster@campus.edu.in", password="Password123!"
        )
        self.student = create_verified_user(
            username="random_student", email="student@campus.edu.in", password="Password123!"
        )
        self.mod_user = create_verified_user(
            username="campus_admin", email="admin@campus.edu.in", password="Password123!"
        )

        mod_group, _ = Group.objects.get_or_create(name="Moderators")
        perm = Permission.objects.get(codename="moderate", content_type__app_label="bounties")
        mod_group.permissions.add(perm)
        self.mod_user.groups.add(mod_group)

        self.bounty = Bounty.objects.create(
            poster=self.poster,
            title="Suspicious Commercial Ad Posting",
            description="Buy cheap assignment solutions here at external website link.",
            category=Category.OTHER,
            reward=500,
            deadline=timezone.now() + timedelta(days=2),
            status=BountyStatus.OPEN,
        )

    def test_student_can_file_abuse_report(self):
        """Verify student can file a report against a suspicious bounty."""
        self.client.force_login(self.student)
        url = reverse("bounties:report", kwargs={"pk": self.bounty.pk})
        resp = self.client.post(
            url,
            {"reason": ReportReason.COMMERCIAL_SPAM, "note": "Commercial advertisement on academic board."},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        report = Report.objects.filter(bounty=self.bounty).first()
        self.assertIsNotNone(report)
        self.assertEqual(report.reporter, self.student)
        self.assertEqual(report.reason, ReportReason.COMMERCIAL_SPAM)
        self.assertFalse(report.resolved)

    def test_regular_user_cannot_access_mod_queue(self):
        """Verify non-moderators get 403 Forbidden on mod queue."""
        self.client.force_login(self.student)
        resp = self.client.get(reverse("bounties:mod_queue"))
        self.assertEqual(resp.status_code, 403)

    def test_moderator_can_access_mod_queue(self):
        """Verify moderator can view mod queue with pending reports and hidden bounties."""
        Report.objects.create(
            bounty=self.bounty,
            reporter=self.student,
            reason=ReportReason.COMMERCIAL_SPAM,
            note="Spam check",
        )
        self.client.force_login(self.mod_user)
        resp = self.client.get(reverse("bounties:mod_queue"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "CAMPUS MODERATION QUEUE")
        self.assertContains(resp, "Suspicious Commercial Ad")

    def test_moderator_can_toggle_bounty_visibility(self):
        """Verify moderator can hide and unhide a bounty."""
        # Non-mod cannot hide
        self.client.force_login(self.student)
        resp_denied = self.client.post(reverse("bounties:toggle_hide", kwargs={"pk": self.bounty.pk}))
        self.assertEqual(resp_denied.status_code, 403)

        # Moderator can hide
        self.client.force_login(self.mod_user)
        resp_hide = self.client.post(reverse("bounties:toggle_hide", kwargs={"pk": self.bounty.pk}), follow=True)
        self.assertEqual(resp_hide.status_code, 200)
        self.bounty.refresh_from_db()
        self.assertTrue(self.bounty.is_hidden)

        # Board excludes hidden bounty
        resp_board = self.client.get(reverse("board"))
        self.assertNotContains(resp_board, "Suspicious Commercial Ad")

        # Moderator can unhide
        resp_unhide = self.client.post(reverse("bounties:toggle_hide", kwargs={"pk": self.bounty.pk}), follow=True)
        self.assertEqual(resp_unhide.status_code, 200)
        self.bounty.refresh_from_db()
        self.assertFalse(self.bounty.is_hidden)

    def test_hidden_bounty_detail_access_rules(self):
        """Hidden bounties return 404 for public/students, 200 for poster and moderator."""
        hide_bounty(self.bounty.pk, self.mod_user, is_hidden=True)
        detail_url = reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})

        # Anonymous -> login redirect (the detail page needs a login; nothing is disclosed)
        self.client.logout()
        resp_anon = self.client.get(detail_url)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertTrue(resp_anon["Location"].startswith(reverse("accounts:login")))

        # Random student -> 404
        self.client.force_login(self.student)
        resp_student = self.client.get(detail_url)
        self.assertEqual(resp_student.status_code, 404)

        # Poster -> 200
        self.client.force_login(self.poster)
        resp_poster = self.client.get(detail_url)
        self.assertEqual(resp_poster.status_code, 200)

        # Moderator -> 200
        self.client.force_login(self.mod_user)
        resp_mod = self.client.get(detail_url)
        self.assertEqual(resp_mod.status_code, 200)
        self.assertContains(resp_mod, "MODERATOR TOOLS")

    def test_moderator_resolve_report_and_hide_bounty(self):
        """Verify resolving report with action=hide_and_resolve marks report resolved and hides bounty."""
        report = Report.objects.create(
            bounty=self.bounty,
            reporter=self.student,
            reason=ReportReason.ACADEMIC_DISHONESTY,
            note="Selling exam leak.",
        )
        self.client.force_login(self.mod_user)
        resolve_url = reverse("bounties:resolve_report", kwargs={"pk": report.pk})
        resp = self.client.post(resolve_url, {"action": "hide_and_resolve"}, follow=True)
        self.assertEqual(resp.status_code, 200)

        report.refresh_from_db()
        self.bounty.refresh_from_db()
        self.assertTrue(report.resolved)
        self.assertTrue(self.bounty.is_hidden)


class RateLimitingVerificationTests(TestCase):
    def test_rate_limited_views_have_ratelimit_decorators(self):
        """Verify rate limits are configured on all required critical endpoints."""
        from apps.accounts.views import login_view, signup_view
        from apps.bounties.views import (
            bounty_claim_view,
            bounty_create_view,
            bounty_report_view,
        )

        for view_fn in [signup_view, login_view, bounty_create_view, bounty_claim_view, bounty_report_view]:
            # django-ratelimit sets ratelimit attribute or wraps function
            has_rl = hasattr(view_fn, "ratelimit") or hasattr(view_fn, "__wrapped__")
            self.assertTrue(has_rl, f"Expected {view_fn.__name__} to be wrapped with ratelimit decorator.")

