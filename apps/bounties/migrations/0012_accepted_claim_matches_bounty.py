"""I2, the half a check constraint cannot express: ``bounty.accepted_claim`` is an ACCEPTED claim
on that same bounty.

It spans two tables, so it is a constraint trigger on each. Both are DEFERRABLE INITIALLY DEFERRED:
a service may write the bounty and the claim in either order, and only the state at commit is judged.
The functions re-read the rows instead of trusting NEW, which is the row as it was at the statement,
not at commit.

This is a backstop for code that bypasses the services (a shell, a data fix, a future bug). Under
READ COMMITTED two transactions that each change only one side could still pass together; the services
prevent that by taking the bounty row lock first.

PostgreSQL only. SQLite is a development convenience and has no deferred triggers.
"""
from django.db import migrations

FORWARD = """
CREATE FUNCTION bounties_check_accepted_claim(target bigint) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM bounties_bounty b JOIN bounties_claim c ON c.id = b.accepted_claim_id
        WHERE b.id = target AND (c.bounty_id <> b.id OR c.status <> 'ACCEPTED')
    ) THEN
        RAISE EXCEPTION 'accepted_claim_matches_bounty: bounty % points at a claim that is not an '
                        'accepted claim on it', target
            USING ERRCODE = 'check_violation', CONSTRAINT = 'accepted_claim_matches_bounty';
    END IF;
END $$;

CREATE FUNCTION bounties_bounty_accepted_claim_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM bounties_check_accepted_claim(NEW.id);
    RETURN NULL;
END $$;

CREATE FUNCTION bounties_claim_accepted_claim_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM bounties_check_accepted_claim(b.id) FROM bounties_bounty b WHERE b.accepted_claim_id = NEW.id;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER accepted_claim_matches_bounty
    AFTER INSERT OR UPDATE OF accepted_claim_id ON bounties_bounty
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION bounties_bounty_accepted_claim_trigger();

CREATE CONSTRAINT TRIGGER accepted_claim_still_matches
    AFTER UPDATE OF status, bounty_id ON bounties_claim
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION bounties_claim_accepted_claim_trigger();

-- Triggers do not look at existing rows, so refuse to install over data that already breaks I2.
SELECT bounties_check_accepted_claim(id) FROM bounties_bounty WHERE accepted_claim_id IS NOT NULL;
"""

REVERSE = """
DROP TRIGGER accepted_claim_still_matches ON bounties_claim;
DROP TRIGGER accepted_claim_matches_bounty ON bounties_bounty;
DROP FUNCTION bounties_claim_accepted_claim_trigger();
DROP FUNCTION bounties_bounty_accepted_claim_trigger();
DROP FUNCTION bounties_check_accepted_claim(bigint);
"""


def run(sql):
    def step(apps, schema_editor):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(sql, params=None)

    return step


class Migration(migrations.Migration):
    dependencies = [("bounties", "0011_notification_reminder_index")]
    operations = [migrations.RunPython(run(FORWARD), run(REVERSE))]
