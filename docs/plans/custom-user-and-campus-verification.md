# Plan: custom user model + campus verification (decisions 1–3)

## Context

Today the app uses `auth.User` (no `apps/accounts/models.py`). Signup gates on `ALLOWED_EMAIL_DOMAIN` with suffix matching (the Phase 0 stop-ship). Contact is free text collected at pitch time (`Claim.contact`). The policy helpers in `apps/bounties/policies.py` only check claim status, not identity. Decisions 1–3 need a custom user model with DB-enforced case-insensitive identity, a verification tier system, and contact released only between two LPU-verified parties. No production data exists, so we reset migrations once, now, before anything ships.

**Decision 2 revised this session (23 Nov):** students also have `@lpu.co.in` addresses, so the email domain is **not a role signal**.
- Verifying an exact-match campus email is the **primary** LPU-verification path for students.
- In-person ID check is the **fallback**.
- The staff role is granted only by a moderator or an allowlist, never inferred from email.

PR 1 updates CLAUDE.local.md to match (decision 2 and invariant I9).

## Verification tiers

| Tier | How | Unlocks |
|---|---|---|
| `UNVERIFIED` | signed up, link not clicked | nothing; login refused (shows a "resend link" option) |
| `EMAIL_VERIFIED` | clicked the link for any-domain email | log in, browse, post (cap 1 open), pitch |
| `LPU_VERIFIED` | an active `CampusVerification` (method `CAMPUS_EMAIL` or `IN_PERSON`). Active means **not revoked AND `verified_at` > now − `VERIFICATION_TTL_DAYS`** (default 365). | accept / be accepted (contact exchange), cap 5 open |
| Staff role (orthogonal) | moderator grant, or `STAFF_EMAIL_ALLOWLIST` match at verification | "Staff" badge on bounties (shown only while also LPU-verified) |

Methods enum: `CAMPUS_EMAIL`, `IN_PERSON`. `VOUCH` is **not** added until it is designed (adding it later is an ordinary migration).

## Data model (`apps/accounts/models.py`)

**`User(AbstractUser)`**
- `email`: required.
- `email_verified_at`: nullable.
- `contact_handle`: optional WhatsApp/phone shown after acceptance; email is always shown. *(Added in PR 6.)*
- `email_token_hash` and `email_token_expires_at` *(added in PR 3)* back a stateful token. It is `secrets.token_urlsafe(32)`, only its sha256 is stored, it lasts 24 h, a resend overwrites it (so the previous token dies), and it is nulled on use (single-use).
- `staff_since`, `staff_source` (`MODERATOR`/`ALLOWLIST`) and `staff_granted_by`. *(Added in PR 4, with the staff check constraint below and the allowlisted staff grant.)*
- Constraints (PR 1 adds the two unique constraints; the staff check is PR 4):
  - `UniqueConstraint(Lower("email"))` and `UniqueConstraint(Lower("username"))`.
  - CheckConstraint: `staff_source IS NULL ⇔ staff_since IS NULL`. *(Added in PR 4.)*
- Manager: normalizes email (trim + lower). `create_superuser` sets `email_verified_at=now()` so admins can log in.

**`StudentIdentity`**: the permanent reg-no binding, for Sybil resistance.
- `reg_no_hmac`: CharField(64), **full unique**. It is `HMAC-SHA256(REG_NO_HMAC_KEY, normalized)`, where normalization strips spaces and dashes and the result must match `REG_NO_PATTERN` (default `^\d{8}$`). The value is always handled as a string, never an int.
- `reg_no_last4`, `user` FK, `bound_at`.
- `source`: `SELF_DECLARED` (typed in on the campus-email path) or `CARD_CHECKED` (a verifier saw the card). Moderators use it in review: a card check outranks a self-declaration.
- **Collected on both paths.** The `CAMPUS_EMAIL` confirm form also asks for the reg no, with the same normalization and HMAC.
- **A collision never blocks** (on either path). If the HMAC is already bound to another user, the binding is inserted inside a savepoint, the savepoint catches the `IntegrityError`, and the verification still goes through with `identity=NULL`. An `IdentityReview` item is opened. A mistyped or squatted reg no therefore can't lock out the real owner. The trade-off is that I9's "one account per reg no" becomes "one *bound* account per reg no; duplicates are flagged for review, not prevented". I'll update I9's wording in CLAUDE.local.md to match.
- The binding outlives revocation, so a revoked user can't re-verify the same card on a new account. Only a moderator `release_identity` (audited) frees it, e.g. when someone verified with another student's number.
- No plaintext and no reversible copy. Moderators look up a student by typing a reg no, which is HMACed and compared; the lookup is audited.

**`IdentityReview`**: the moderator queue for reg-no collisions.
- Fields: `claimant` (the newcomer), `existing_identity` FK, `claimed_hmac`, `claimed_last4`, `claimant_source`, `status` (`OPEN`/`RESOLVED`), `resolution`, `resolved_by`, `resolved_at`, `note`.
- `resolution` values:
  - `KEEP_EXISTING`: the newcomer is asked to re-enter, or a moderator revokes them with `VERIFICATION_INVALID`.
  - `REASSIGN`: release the binding, rebind it to the newcomer, and revoke the old holder with `VERIFICATION_INVALID`.
  - `BOTH_LEGIT_TYPO`: the newcomer re-enters their reg no.
- Partial unique `(claimant) WHERE status='OPEN'`: at most one open review per user.
- Resolutions run through a service, inside one transaction with an `AuditEvent`. The moderator queue lists open reviews.

**`CampusVerification`**: one row per verification event.
- `user`, `method`, `verified_at`, `verified_by` (null for `CAMPUS_EMAIL`).
- `verified_email`: a snapshot, for `CAMPUS_EMAIL`.
- `identity` FK: for `IN_PERSON`.
- `card_name_enc`: Fernet ciphertext of the name as printed on the card. `IN_PERSON` only.
- `revoked_at`, `revoked_by`, `revoke_reason`, `revoke_note`.
- `revoke_reason` values: `LEFT_CAMPUS`, `VERIFICATION_INVALID`, `POLICY_VIOLATION`, plus three system-only reasons:
  - `EMAIL_CHANGED`.
  - `EXPIRED`: set by the sweeper. For permissions it is treated exactly like `LEFT_CAMPUS`, and it never notifies counterparties.
  - `SUPERSEDED`: the row was closed by a renewal.
- `renewal_reminder_sent_at`: nullable. It stops a second reminder for the same row.
- **Expiry is time-based in the policy, not sweeper-dependent.** `is_lpu_verified` checks `revoked_at IS NULL AND verified_at > now − TTL`, so a sleeping Render instance or a missed tick can't extend access. The sweeper (in `/internal/tick`) only closes lapsed rows with `EXPIRED` + `AuditEvent`, so that the one-unrevoked-row partial unique stays true and renewal can insert a fresh row.
- Constraints:
  - Partial unique `(user) WHERE revoked_at IS NULL`: at most one active verification per user.
  - CheckConstraints, per method:
    - `IN_PERSON` ⇒ `verified_by` and `card_name_enc` are NOT NULL. (`identity` may be NULL: a reg-no collision verifies the user unbound, see "Deviations from this plan".)
    - `CAMPUS_EMAIL` ⇒ `verified_email` NOT NULL, and `verified_by` and `identity` NULL.
  - CheckConstraint: `revoked_at IS NULL ⇔ revoke_reason IS NULL`.
- Permission: none on this model. `accounts.verify_student` lives permanently on `User.Meta.permissions` (added in PR 1) and is not moved here.
- **Card name:**
  - The column is `defer()`-ed in the default manager and never appears in a form, template context or `__str__`.
  - It is decrypted only by `services.reveal_card_name()`: a moderator does it with a reason, which writes an audit row; the owning user can see their own on `/me`.
  - It is deleted with the account (CASCADE).
  - Key: `CARD_NAME_KEYS`, a comma list for `MultiFernet` rotation. Encrypting means a leaked `pg_dump` artifact or Supabase dump doesn't reveal names.
  - New dependency: `cryptography`.

**`AuditEvent`** (append-only; admin is read-only, with no change or delete permission)
- Fields: `actor` and `subject` FKs (SET_NULL), `action`, `reason`, `note`, `verification` FK nullable, `created_at`.
- `action` values: `VERIFIED`, `REVOKED`, `IDENTITY_RELEASED`, `STAFF_GRANTED`, `STAFF_REVOKED`, `CONTACT_VIEWED`, `CARD_NAME_VIEWED`, `REG_NO_LOOKUP`.
- This one table covers "log every revocation" and "moderator access to contact or card name is audited".

**Bounties changes** (normal migrations after the reset)
- Drop `Claim.contact`.
- Add `ClaimStatus.CANCELLED` for the revocation-cancel path.

## Services (`apps/accounts/services.py`; lock rule: users locked in ascending id order, always after any bounty/claim locks)

- **`send_verification_email(user)` / `confirm_email(token)`**
  - The link is `/accounts/verify-email/<uid>/<token>/`. The token hash is bound to the user id and password hash, so replacing the account or changing the password kills older links.
  - The GET page shows the address and a password field and never mutates (Gmail/Outlook link scanners prefetch GETs). Only the POST mutates, and only with the right account password: the link alone proves mailbox access, not that the account is the reader's (pre-hijacking). On success the session is rotated and the user is logged in. The POST shares login's throttle (same per-IP bucket, same per-account failure budget).
  - **`signup(username, email, password)`**: under a per-address advisory lock, an address held by an *unverified* account has that account deleted and replaced; a *verified* one is never touched and its owner gets a "you already have an account" email, with a response identical to a new address (no enumeration). Signup and resend share one per-address email budget. Stale unverified accounts (older than the link lifetime, no live link, no owned rows, no role) are purged lazily on collision and by `purge_unverified_accounts`.
  - On confirm: set `email_verified_at`. If the domain is in `ALLOWED_CAMPUS_DOMAINS` (exact domain match; trimmed and lowercased; default `["lpu.co.in"]`):
    - The confirm form also requires the reg no, unless the user already holds a binding.
    - Create a `CAMPUS_EMAIL` verification and close any prior unrevoked row with `SUPERSEDED`.
    - Bind the identity, or open an `IdentityReview` on collision.
  - If the email is in `STAFF_EMAIL_ALLOWLIST`, grant the staff role (`ALLOWLIST`).
  - **Renewal uses this same flow:** a re-verify email is just a fresh token to the campus address.
- **`sweep_verifications(now)`**, called from `/internal/tick`:
  1. Close lapsed rows with `EXPIRED`. No counterparty notifications.
  2. Send renewal reminders `RENEWAL_REMINDER_DAYS` (default 30) before lapse, oldest-first, capped at `RENEWAL_REMINDERS_PER_DAY` (default 50), because email is limited to 300/day and verification, reset and acceptance mail take priority.
  - `CAMPUS_EMAIL` users get a one-click re-verify link: a GET page with one POST button, because of link scanners. `IN_PERSON` users get a "visit a desk or verify a campus email" reminder; see the open question.
  - Reminders go through the outbox.
- **`change_email(user, new_email)`**
  - Sets `email_verified_at=NULL` and revokes any active verification with `EMAIL_CHANGED`. The `StudentIdentity` binding stays.
  - Email is read-only in admin.
  - A middleware logs out any authenticated user whose `email_verified_at` is NULL (fail closed).
  - The user-facing "change email" page is deferred; the service and its test land now.
- **`verify_in_person(verifier, username, presence_code, reg_no, card_name)`**
  - The student's `/me` shows a 6-digit code, `HMAC(SECRET_KEY, user_id ‖ 10-min window)`. This is stateless and proves the student is at the desk. The verifier sees only the username, never the email or contact.
  - Locks the verifier and the subject in id order.
  - **Per-verifier rate limit:** a DB count of the verifier's `VERIFIED` events in the last hour and day, taken under the verifier's row lock. Defaults are `VERIFIER_LIMIT_HOURLY=30` and `DAILY=150`. A DB count survives restarts and doesn't depend on LocMem.
  - Inserts `StudentIdentity` (`CARD_CHECKED`) inside a savepoint. On `IntegrityError`, the verification still goes through and an `IdentityReview` is opened (same rule as the email path). If the user already has a `SELF_DECLARED` binding with the same HMAC, it is upgraded to `CARD_CHECKED`.
- **`resolve_identity_review(moderator, review, resolution, note)`**: locks the users involved in id order and writes an `AuditEvent`.
- **`revoke_verification(moderator, user, reason, note)`**
  - Locks the subject user row. `accept_claim` locks the same row, so accept and revoke serialize.
  - Writes `AuditEvent`. Existing CLAIMED bounties and contact already shown are left alone.
  - For `VERIFICATION_INVALID`/`POLICY_VIOLATION`, it writes an in-app `Notification` to the counterparty of each active CLAIMED bounty.
  - Suspending the account is a separate action and is not built here.
- **`release_identity`, `grant_staff` / `revoke_staff`, `lookup_by_reg_no`, `reveal_card_name`, `reveal_contact_for_moderation`**: moderator-only, and each writes an `AuditEvent`.

Bounty service changes (`apps/bounties/services.py`):
- **`accept_claim`:** after the bounty → claims locks, lock the poster and claimant user rows (id order) and require both to be LPU-verified **under the lock** (I9).
- **`create_bounty`:** the cap comes from `policies.open_bounty_cap(user)` and is read under the existing user lock. The count now **includes hidden bounties**: today `Bounty.clean` filters `is_hidden=False`, which contradicts I4. The hardcoded 5 moves out of `Bounty.clean` into the service.
- **`create_claim`:** loses the `contact` arg.
- **New `cancel_after_counterparty_revocation(bounty_id, user)`:**
  - Transition: CLAIMED → CANCELLED. The accepted claim → `CANCELLED` and `accepted_claim` is set to NULL, which keeps I2.
  - Allowed only for the other party, and only when the revoked party has no active verification and its latest revocation reason is INVALID or POLICY.
  - No penalty is recorded.
- **Domain exceptions:** add `NotPermitted` next to `InvalidTransition` in a shared `apps/exceptions.py`. Services stop raising `PermissionDenied`/`ValidationError`.

## Policies (`apps/bounties/policies.py` stays the single module)

- **Tiers:** `verification_tier(user, now=None)`, `is_email_verified`, `is_lpu_verified` (not revoked AND within TTL), `has_staff_badge(user)` (staff role AND LPU-verified). A lapsed user gets exactly the `LEFT_CAMPUS` permissions: no accept, cap 1, existing CLAIMED bounties untouched.
- **Bounties:** `open_bounty_cap(user)` (0 / 1 / 5), `can_post_bounty`, `can_pitch(user, bounty)`.
- **Acceptance:** `can_accept_claim(user, claim)`, used by the UI. The service re-checks under lock. The UI tells the poster when a pitcher is not yet LPU-verified.
- **Contact:** `can_view_contact(viewer, subject, bounty)` replaces `can_view_claim_contact`/`can_view_poster_contact`. It is true only when the bounty has an ACCEPTED claim and viewer and subject are its two parties. Contact comes from the profile (`email` + `contact_handle`).
- **Revocation cancel:** `can_cancel_after_revocation(user, bounty)`.
- **Roles:** `can_moderate` is `has_perm("bounties.moderate")`. The `or is_staff` shortcut is dropped; superusers still pass. `can_verify_students` is `has_perm("accounts.verify_student")`.
- **Call sites:** these replace the scattered checks at `apps/bounties/views.py:116,135,354,380,400`, `apps/accounts/views.py:116`, `services.py:241,256` and `templates/base.html:73`, `detail.html:268`. Templates get booleans from views.

## Access gating

- Add Django 5.1's `LoginRequiredMiddleware`.
- `@login_not_required` goes only on: landing, about, terms, privacy, guidelines, login, signup, verify-email, password reset, `healthz`, `robots.txt`, `sitemap.xml` and (later) `/internal/tick`.
- `/` becomes a public landing page; the board moves to `/board/`. Authenticated users hitting `/` are redirected to the board.
- The sitemap lists public pages only.
- This also removes anonymous detail-page session writes (Phase 1 item).

## Migration reset (PR 1, done once)

1. Delete `apps/accounts/migrations/0001_initial.py` and `apps/bounties/migrations/0001_initial.py`. Decision 1 authorizes the reset; approving this plan is the ask for the file deletions.
2. Add `AUTH_USER_MODEL = "accounts.User"` in `config/settings/base.py`.
3. Run `makemigrations`. This produces `accounts/0001` (User), then `bounties/0001` (depends on the swappable user), then `accounts/0002_groups`: a data migration that recreates **Moderators** (`bounties.moderate`) and creates **Verifiers** (`accounts.verify_student` only), reusing the `get_or_create` pattern from the current `accounts/0001`.
4. Recreate the local DB: `dropdb bountyboard && createdb bountyboard`. The test DB rebuilds itself.
5. Before the first deploy, check whether Supabase already has `django_migrations` rows. If so, it needs `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`. That is destructive, so I'll **ask before running it**.
6. Add a test that runs `makemigrations --check --dry-run` so model/migration drift fails CI.
7. Update `seed_demo` if the user swap requires it. **The signup domain gate stays in PR 1** (decided 23 Nov): `email_domain_allowed`, `ALLOWED_EMAIL_DOMAIN`, `docs/audit/test_audit_repro.py` and the tests at `apps/accounts/tests.py:69-218` are untouched. **PR 3 adds email verification on top of the existing gate.** **PR 4 removes the gate** (the suffix-match function and `ALLOWED_EMAIL_DOMAIN`) and introduces `ALLOWED_CAMPUS_DOMAINS` together with the campus check, so open signup never exists without that check.

New prod-required settings (fail closed in `config/settings/prod.py`): `REG_NO_HMAC_KEY` and `CARD_NAME_KEYS`. `ALLOWED_CAMPUS_DOMAINS` replaces `ALLOWED_EMAIL_DOMAIN` (both in PR 4), including in `apps/pages/context_processors.py` and `.env.example`. These are separate from `SECRET_KEY`, so rotating it doesn't break reg-no uniqueness. Key handling goes in the commit body: if the HMAC key leaks along with the DB, an 8-digit space is brute-forced in seconds, so the key lives only in Render env, never in backups.

## PR breakdown (each: small commits, tests in the same commit, full Postgres suite green, "not covered" list)

1. **Custom user + migration reset.** Adds User, the case-insensitive unique constraints, `email_verified_at` (existing behaviour otherwise unchanged; signup still logs in), the groups migration, the drift test, admin and seed. Tests: an `IntegrityError` on `A@x.com` vs `a@x.com` and on usernames, at the ORM `bulk_create` level so form checks are bypassed. Also updates CLAUDE.local.md decision 2 / I9.
2. **Policies + domain exceptions.** A pure refactor: `can_moderate`, `NotPermitted`, and replacing the scattered checks. No behaviour change except dropping `is_staff`-as-moderator, which gets a test.
3. **Email verification** on top of the existing signup domain gate (the gate is **not** removed here). Covers the token flow, POST-confirm, resend rate-limited per address and IP, login refused for unverified accounts, the logout middleware, `LoginRequiredMiddleware`, the landing page and `/board/`, and the robots/sitemap scope. `CAMPUS_EMAIL` auto-verification is **not** in this PR (no model yet).
4. **Campus verification model + services.** Adds `StudentIdentity`, `CampusVerification`, `IdentityReview`, `AuditEvent`, and `CAMPUS_EMAIL` on confirm (the reg-no field, plus a review on collision). TTL is enforced in the policy from day one. Also **removes the signup domain gate and introduces `ALLOWED_CAMPUS_DOMAINS`** together with the campus check (closes Phase 0), and adds the `User` staff fields (`staff_since`, `staff_source`, `staff_granted_by`) with their check constraint, the allowlisted staff grant, `verify_in_person` with the rate limit, `revoke_verification`, `release_identity`, card-name encryption and the prod settings. `accounts.verify_student` is already on `User` (PR 1); no move. Admin is read-only; its actions call services.
5. **Verifier desk UI + presence code.** Adds the Verifiers-only view (username + code + reg no + card name), the code on `/me`, and the own-record view on `/me`. Tests show that a verifier can't reach moderation or contact pages.
6. **Gating at the moment of value.**
   - `accept_claim` requires both parties verified, under lock.
   - Caps of 1 and 5, with the hidden-count fix.
   - Drop `Claim.contact` and the pitch-time contact field.
   - Profile `contact_handle`, the unified `can_view_contact`, and the pre-accept views/email no longer show the claimant email.
   - Copy updates in the `notifications.py` accept email, `me.html` and `detail.html`.
7. **Revocation consequences + moderator tools + staff badge + terms.**
   - The counterparty notification and the `cancel_after_counterparty_revocation` transition.
   - Moderator reg-no lookup, contact reveal and card-name reveal (all audited), and staff grant/revoke.
   - The "Staff" badge.
   - Terms and privacy copy: tasks are private arrangements, not university work, plus what we store about IDs.
   - The moderator `IdentityReview` queue and resolution.
   - **Dependency:** the in-app `Notification` row. If the outbox (I7) hasn't landed by then, this PR adds just the `Notification` table in the outbox's shape, in-app only, with no delivery loop.
8. **Expiry sweep + renewal reminders.**
   - `sweep_verifications` (`EXPIRED` + `SUPERSEDED` handling), the reminder batching with a daily cap, and the one-click renewal page.
   - **Dependency:** `/internal/tick` and outbox email delivery. This PR lands after those. Until then, expiry is still enforced, because the policy check is time-based and PR 4 ships it.

## Tests that must fail if enforcement is removed

- Per constraint: `IntegrityError` tests for the lower(email)/lower(username) unique constraints, the one-active-verification partial unique, `reg_no_hmac` unique, and the per-method checks.
- I9: `accept_claim` with an unverified party raises `NotPermitted`. A deterministic barrier test runs revoke against accept and asserts that either the accept fails or the revoke happens after it, never contact released to a revoked user. Thread DB connections are closed.
- Verifier rate limit: the N+1th verification in an hour fails (behavioural, not a tautology).
- Card name: it never appears in rendered detail, board or `/me` pages of other users, or in admin changelist HTML. Decryption round-trips under key rotation.
- Reg-no collision (both paths): the second user is verified with `identity=NULL`, exactly one `IdentityReview` is OPEN, and the first binding is unchanged. The test fails if the savepoint is removed: the outer transaction aborts and the verification is lost.
- Expiry, with a frozen clock at TTL + 1 s: `accept_claim` raises `NotPermitted` and the cap is 1. This holds **without running the sweeper**. The sweeper closes the row with `EXPIRED` and sends no counterparty notification. Renewal inserts a new row and supersedes the old one. Reminders respect the daily cap and are sent at most once per row.
- Email token: single-use, expired after 24 h (frozen clock), invalidated by a resend, and a GET doesn't consume it.
- Coverage gate stays at its current ratchet or higher.

## Verification

```bash
DATABASE_URL=postgres://localhost/bountyboard python manage.py test --settings=config.settings.dev
python manage.py makemigrations --check --dry-run --settings=config.settings.dev
coverage run --branch manage.py test --settings=config.settings.dev && coverage report --omit=...
```

Manual run-through via `runserver` and the console email backend (signup is open to any domain from PR 4; `lpu.co.in` is just the default `ALLOWED_CAMPUS_DOMAINS`):
1. Sign up with an `@lpu.co.in` address, then confirm the email.
2. Post a bounty; a second post is refused (cap 1).
3. Sign up a second user with `@lpu.co.in`; they become LPU-verified on confirm (PR 4 on).
4. That user pitches; the poster can't accept because the poster isn't LPU-verified.
5. A verifier verifies the poster in person with the presence code.
6. The poster accepts; both parties see each other's contact.
7. A moderator revokes with `POLICY_VIOLATION`; the counterparty sees the notification and cancels.

**PR 4 run-through addition:** sign up with a gmail address and confirm the email. They become `EMAIL_VERIFIED` only: they can post (cap 1) and pitch, but cannot accept or be accepted.

## Deviations from this plan (found while building PR 4)

1. **`IN_PERSON` does not require `identity`.** The original check contradicted "a collision never blocks": the colliding user is verified with `identity=NULL`, so the constraint would have rejected the insert and lost the verification. `CAMPUS_EMAIL` rows also forbid `verified_by`.
2. **`verified_by` is `PROTECT`**, not `SET_NULL` (which would violate the `IN_PERSON` check): a verifier with history cannot be deleted.
3. **A card that disagrees with an already-bound number** (a different `reg_no_hmac` for the same user) was unspecified. The binding is left untouched, the verification still goes through, and an `IdentityReview` is opened for a moderator.
4. **`ALLOWED_CAMPUS_DOMAINS` landed one commit before the gate removal**, because the confirm path needs it. Open signup never existed without the campus check.
5. **Accounts errors** live in `apps/accounts/errors.py` (shaped like PR 2's `DomainError`) until PR 2 merges; then re-parent them. The moderator check is a local helper with `can_moderate`'s rule, to be swapped likewise.
6. **Open question 3 is not implemented yet.** `StudentIdentity.user` is a non-null `OneToOne(CASCADE)`, so deleting an account always deletes its binding, including after a `POLICY_VIOLATION`. Keeping a banned student's binding needs `user` nullable plus a deletion service; that is separate work.
7. **No admin actions yet.** The campus models are view-only in the admin; moderator actions arrive as views in PR 7.

## Open questions (my default, if you don't say otherwise)

1. **Renewal for `IN_PERSON` users.** They have no campus inbox, so the "one-click re-verify" can't prove anything. My default: their reminder email asks them to visit a desk, or to add and verify an `@lpu.co.in` address. A one-click "I'm still on campus" self-attestation is **not** offered, because it would make the TTL meaningless.
2. **Remaining Sybil gap.** A self-declared reg no on the email path is unverified. Someone could type another student's number first, and the real owner then lands in review instead of being blocked. That is the intended trade-off. It goes in the "not covered" list: a review backlog during launch week could delay legitimate students' bindings, though not their verification.
3. **Account deletion vs. the binding.** The card name goes with the account (as you asked). The default for the `StudentIdentity` row is to delete it too, *unless* the user's last revocation was `POLICY_VIOLATION`, in which case it stays with `user=NULL` so a banned student can't come back by deleting and re-signing up.
