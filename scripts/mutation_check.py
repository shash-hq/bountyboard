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
    ("create rate limit", VIEWS, after("bounty_create_view"), "", LIMITS),
    ("claim rate limit", VIEWS, after("bounty_claim_view"), "", LIMITS),
    ("report rate limit", VIEWS, after("bounty_report_view"), "", LIMITS),
    ("signup rate limit", "apps/accounts/views.py", after("signup_view"), "", ["apps.accounts.tests_ratelimits"]),
    # The latest migration that defines it: removing an earlier, superseded definition changes nothing.
    ("I2 check constraint", "apps/bounties/migrations/0008_expired_in_accepted_claim_constraint.py",
     r"(?s)        migrations\.AddConstraint\(.*?(?=    \]\n)", "", DB),
    ("I1 unique index", CONSTRAINTS,
     r"(?s)        migrations\.AddConstraint\(\n            model_name=\"claim\".*?(?=    \]\n)", "", DB),
]


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
        baseline = run_tests(tree, sorted({label for *_, labels in MUTATIONS for label in labels}))
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
            # Require a test run that finished and failed; an import error would prove nothing.
            killed = result.returncode != 0 and re.search(r"^FAILED \(", result.stderr, re.M)
            print(f"{'caught  ' if killed else 'SURVIVED'} {name}", flush=True)
            if not killed:
                survivors.append(name)
    if survivors:
        sys.exit(f"{len(survivors)} protection(s) can be removed without a test failing: {', '.join(survivors)}")
    print(f"All {len(MUTATIONS)} mutations caught.")


if __name__ == "__main__":
    main()
