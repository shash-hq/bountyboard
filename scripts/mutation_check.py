"""Remove each lock, constraint and rate limit in turn and require the tests to go red.

Works on a temporary copy of the tree, so the working copy is never modified. Needs PostgreSQL
(DATABASE_URL), like the race tests. Every pattern must match exactly once, so moving the code
fails this script loudly instead of silently skipping a check.

    DATABASE_URL=postgres://localhost/bountyboard python scripts/mutation_check.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = "apps/bounties/services.py"
VIEWS = "apps/bounties/views.py"
CONSTRAINTS = "apps/bounties/migrations/0003_accepted_claim_constraints.py"

RACES = ["apps.bounties.tests_race_anomalies", "apps.bounties.tests_races"]
LIMITS = ["apps.bounties.tests_ratelimits"]
DB = ["apps.bounties.tests_db_constraints"]


def after(def_name):
    """A regex for the first rate-limit decorator above ``def_name``."""
    return rf"@ratelimit\([^\n]*\)\n(?=(?:@[^\n]*\n)*def {def_name}\()"


# (name, file, regex, replacement, tests that must fail)
MUTATIONS = [
    ("bounty row lock", SERVICES, r"Bounty\.objects\.select_for_update\(no_key=True\)\.get\(", "Bounty.objects.get(", RACES),
    ("poster lock in create_bounty", SERVICES, r"User\.objects\.select_for_update\(\)\.get\(pk=poster\.pk\)",
     "User.objects.get(pk=poster.pk)", RACES),
    ("verifier lock at the desk", "apps/accounts/desk.py",
     r"User\.objects\.select_for_update\(no_key=True\)(?=\.filter\(pk__in=\[verifier_id, subject_id\]\))",
     "User.objects", ["apps.accounts.tests_lock_gaps"]),
    ("user lock in confirm_email", "apps/accounts/services.py",
     r"User\.objects\.select_for_update\(\)(?=\.filter\(pk=uid, is_active=True\))", "User.objects",
     ["apps.accounts.tests_lock_gaps"]),
    ("holder lock in signup", "apps/accounts/services.py",
     r"User\.objects\.select_for_update\(\)(?=\.filter\(email__iexact=email\))", "User.objects",
     ["apps.accounts.tests_lock_gaps"]),
    ("user locks in accept_claim", SERVICES,
     r"User\.objects\.select_for_update\(no_key=True\)(?=\.filter\(pk__in=\{poster_id, claimant_id\})",
     "User.objects", ["apps.bounties.tests_accept_gating"]),
    ("user locks in cancel_after_counterparty_revocation", SERVICES,
     r"User\.objects\.select_for_update\(no_key=True\)(?=\.filter\(\s*pk__in=\{bounty\.poster_id, claim\.claimant_id\})",
     "User.objects", ["apps.bounties.tests_revocation_cancel"]),
    # Strengthening these back to FOR UPDATE reintroduces the revocation deadlocks.
    ("bounty lock strength", SERVICES, r"(?<=Bounty\.objects\.select_for_update\()no_key=True(?=\))", "",
     ["apps.bounties.tests_revocation_deadlocks"]),
    ("user lock strength in accept_claim", SERVICES,
     r"(?<=User\.objects\.select_for_update\()no_key=True(?=\)\.filter\(pk__in=\{poster_id)", "",
     ["apps.bounties.tests_revocation_deadlocks"]),
    ("user lock strength in revoke_verification", "apps/accounts/moderation.py",
     r"(?<=select_for_update\()no_key=True(?=\)\.filter\(pk__in=\{actor_id, subject_id\}\)\.order_by\(\"pk\"\)\}\n    actor, subject = rows\.get\(actor_id\), rows\.get\(subject_id\)\n    # Permission)",
     "", ["apps.bounties.tests_revocation_deadlocks"]),
    ("outbox SKIP LOCKED", "apps/bounties/outbox.py", r"skip_locked=True, ", "", ["apps.bounties.tests_outbox"]),
    # Without of=("self",), select_related also locks the claimants' user rows FOR UPDATE.
    ("claim-only lock in cancel and expire", SERVICES,
     r'(?s)of=\("self",\)(\)\.select_related\("claimant"\)(?:.*?))of=\("self",\)(?=\)\.select_related\("claimant"\))', r"\1",
     ["apps.bounties.tests_revocation_deadlocks"]),
    ("claim state check before emailing", "apps/bounties/emails.py", r"claim\.status != ClaimStatus\.ACCEPTED",
     "False", ["apps.bounties.tests_outbox_safety"]),
    ("daily cap lock", "apps/bounties/outbox.py", r"if not _take_budget_lock\(\):", "if False:",
     ["apps.bounties.tests_outbox_safety"]),
    ("reminder still true before emailing", "apps/bounties/emails.py",
     r"if not campus\.active_verifications\(\)\.filter\(pk=verification, user=user\)\.exists\(\):", "if False:",
     ["apps.accounts.tests_renewal_reminders_safety"]),
    ("block at campus-email verification", "apps/accounts/campus.py", r" or is_blocked\(user\):", ":",
     ["apps.accounts.tests_verification_block"]),
    ("block at the verification desk", "apps/accounts/desk.py", r"if campus\.is_blocked\(subject\):", "if False:",
     ["apps.accounts.tests_verification_block"]),
    ("block on renewal", "apps/accounts/renewal.py", r"if campus\.is_blocked\(user\):", "if False:",
     ["apps.accounts.tests_verification_block"]),
    ("revoking an expired verification", "apps/accounts/moderation.py",
     r"return latest if latest is not None and latest\.revoke_reason == RevokeReason\.EXPIRED else None",
     "return None", ["apps.accounts.tests_lapsed_revocation"]),
    ("block_cleared_at check constraint", "apps/accounts/migrations/0009_verification_block.py",
     r"(?s)        migrations\.AddConstraint\(.*?(?=        migrations\.AlterField\()", "",
     ["apps.accounts.tests_lapsed_revocation"]),
    ("reminder budget lock", "apps/accounts/expiry.py", r"cursor\.execute\(REMINDER_BUDGET_LOCK_SQL\)", "pass",
     ["apps.accounts.tests_renewal_reminders_safety"]),
    ("terms guard once claimed", SERVICES, r"if Claim\.objects\.filter\(bounty=bounty\)\.exists\(\):", "if False:",
     ["apps.bounties.tests_edit_terms"]),
    ("bounty row lock, as the terms guard relies on it", SERVICES,
     r"Bounty\.objects\.select_for_update\(no_key=True\)\.get\(", "Bounty.objects.get(",
     ["apps.bounties.tests_edit_terms"]),
    ("poster lock in update_bounty_details", SERVICES,
     r"User\.objects\.select_for_update\(no_key=True\)\.get\(pk=bounty\.poster_id\)", "pass",
     ["apps.bounties.tests_expiry"]),
    ("create rate limit", VIEWS, after("bounty_create_view"), "", LIMITS),
    ("claim rate limit", VIEWS, after("bounty_claim_view"), "", LIMITS),
    ("report rate limit", VIEWS, after("bounty_report_view"), "", LIMITS),
    ("signup rate limit", "apps/accounts/views.py", after("signup_view"), "", ["apps.accounts.tests_ratelimits"]),
    # The latest migration that defines it: removing an earlier, superseded definition changes nothing.
    ("I2 check constraint", "apps/bounties/migrations/0008_expired_in_accepted_claim_constraint.py",
     r"(?s)        migrations\.AddConstraint\(.*?(?=    \]\n)", "", DB),
    ("I1 unique index", CONSTRAINTS,
     r"(?s)        migrations\.AddConstraint\(\n            model_name=\"claim\".*?(?=    \]\n)", "", DB),
    ("I2 accepted-claim triggers", "apps/bounties/migrations/0012_accepted_claim_matches_bounty.py",
     r"run\(FORWARD\), run\(REVERSE\)", "migrations.RunPython.noop", DB),
]


# Database constraints are dropped by an extra migration in the copy rather than by editing the
# migration that created them, so this keeps working however the migrations are reorganised. A name
# that no longer exists makes the migration fail before any test runs, which is reported as SURVIVED.
DROP_BY_NAME = (
    "DO $$ DECLARE t regclass; BEGIN SELECT conrelid::regclass INTO t FROM pg_constraint WHERE conname = '{0}'; "
    "IF t IS NULL THEN EXECUTE 'DROP INDEX {0}'; ELSE EXECUTE format('ALTER TABLE %s DROP CONSTRAINT {0}', t); "
    "END IF; END $$;"
)
DROP_UNIQUE_COLUMN = (
    "DO $$ DECLARE c text; BEGIN SELECT conname INTO STRICT c FROM pg_constraint WHERE conrelid = '{0}'::regclass "
    "AND contype = 'u' AND pg_get_constraintdef(oid) = 'UNIQUE ({1})'; "
    "EXECUTE format('ALTER TABLE {0} DROP CONSTRAINT %I', c); END $$;"
)
MODELS = ["apps.accounts.tests_campus_models"]
# (name, app, sql, tests that must fail)
CONSTRAINT_DROPS = [
    *(
        (name, "accounts", DROP_BY_NAME.format(name), labels)
        for name, labels in (
            ("accounts_user_email_ci_uniq", ["apps.accounts.tests_user_model"]),
            ("accounts_user_username_ci_uniq", ["apps.accounts.tests_user_model"]),
            ("accounts_user_staff_source_iff_since", MODELS),
            ("accounts_user_deleted_is_inactive", MODELS),
            ("accounts_campusverification_one_active_per_user", MODELS),
            ("accounts_campusverification_revoked_iff_reason", MODELS),
            ("accounts_campusverification_in_person_fields", MODELS),
            ("accounts_campusverification_campus_email_fields", MODELS),
            ("accounts_identityreview_one_open_per_claimant", MODELS),
            ("accounts_identityreview_resolved_iff_resolution", MODELS),
        )
    ),
    ("unique_bounty_claimant", "bounties", DROP_BY_NAME.format("unique_bounty_claimant"),
     ["apps.bounties.tests_db_claim_unique"]),
    *(
        (name, "bounties", DROP_BY_NAME.format(name), ["apps.bounties.tests_outbox"])
        for name in ("notification_pending_iff_next_attempt", "notification_sent_has_delivered_at")
    ),
    ("unique reg_no_hmac", "accounts", DROP_UNIQUE_COLUMN.format("accounts_studentidentity", "reg_no_hmac"),
     ["apps.accounts.tests_campus_email"]),
    ("unique idempotency_key", "bounties", DROP_UNIQUE_COLUMN.format("bounties_notification", "idempotency_key"),
     ["apps.bounties.tests_inbox"]),
]


def add_migration(tree, app, sql):
    """Write a migration that runs ``sql`` after the app's current leaf, and return its path."""
    directory = tree / "apps" / app / "migrations"
    names = {p.stem for p in directory.glob("0*.py")}
    needed = {n for n in names for p in directory.glob("0*.py") if f'("{app}", "{n}")' in p.read_text()}
    leaves = sorted(names - needed)
    if len(leaves) != 1:
        sys.exit(f"{app}: expected one leaf migration, found {leaves}")
    path = directory / "9999_mutation.py"
    path.write_text(
        "from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n"
        f"    dependencies = [({app!r}, {leaves[0]!r})]\n    operations = [migrations.RunSQL({sql!r})]\n"
    )
    return path


def report(name, result, survivors):
    # Require a test run that finished and failed; an import error would prove nothing.
    killed = result.returncode != 0 and re.search(r"^FAILED \(", result.stderr, re.M)
    print(f"{'caught  ' if killed else 'SURVIVED'} {name}", flush=True)
    if not killed:
        survivors.append(name)


def run_tests(tree, labels):
    return subprocess.run(
        [sys.executable, "manage.py", "test", *labels, "--settings=config.settings.test", "--noinput", "--failfast"],
        cwd=tree, capture_output=True, text=True, timeout=600,
    )


def main():
    if not os.environ.get("DATABASE_URL", "").startswith("postgres"):
        sys.exit("DATABASE_URL must point at PostgreSQL: SQLite ignores row locks.")
    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.sqlite3", ".env"))

        # A suite that is already red would "kill" every mutation, so require green first.
        every = [*MUTATIONS, *CONSTRAINT_DROPS]
        baseline = run_tests(tree, sorted({label for *_, labels in every for label in labels}))
        if baseline.returncode:
            sys.exit("Unmutated tests fail, so the mutation results would be meaningless:\n" + baseline.stderr[-4000:])

        for name, rel, pattern, replacement, labels in MUTATIONS:
            path = tree / rel
            original = path.read_text()
            mutated, count = re.subn(pattern, replacement, original)
            if count != 1:
                sys.exit(f"{name}: pattern matched {count} times in {rel}; update this script.")
            path.write_text(mutated)
            result = run_tests(tree, labels)
            path.write_text(original)
            report(name, result, survivors)

        for name, app, sql, labels in CONSTRAINT_DROPS:
            extra = add_migration(tree, app, sql)
            report(f"constraint {name}", run_tests(tree, labels), survivors)
            extra.unlink()
    if survivors:
        sys.exit(f"{len(survivors)} protection(s) can be removed without a test failing: {', '.join(survivors)}")
    print(f"All {len(MUTATIONS) + len(CONSTRAINT_DROPS)} mutations caught.")


if __name__ == "__main__":
    main()
