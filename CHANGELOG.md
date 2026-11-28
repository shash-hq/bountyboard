# Changelog

All notable changes to the **Bounty Board** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Accounts:** custom user model with case-insensitive unique email and username, email verification by link, and login required outside the public pages.
- **Campus verification:** by campus email or at an in-person desk, with expiry, renewal reminders, revocation, a block after revocation for cause, and a moderator review queue for duplicate registration numbers.
- **Expiry:** overdue bounties become `EXPIRED`.
- **Notifications:** a transactional outbox, drained by `/internal/tick`, which also expires bounties and verifications and purges abandoned signups.
- **Database enforcement:** constraints and triggers behind the claim and bounty state rules.
- **Checks:** race, state-machine and mutation tests on PostgreSQL, hashed lockfiles, `make bench`.
- **Deployment:** `render.yaml` for a Render web service, and a runbook for scheduling the tick from Supabase.

### Changed
- Accepting a claim requires both users to be campus-verified. Contact details come from the profile and are shown to the two parties only after acceptance. A moderator can also reveal them, with a reason, and that is audited.
- The open-bounty cap is 1 for email-verified users and 5 for campus-verified users.
- Signup accepts any email domain; the domain no longer gates access.
- After a bounty has a claim, its poster can only improve the offer.

### Fixed
- Open redirects, contact leaked before acceptance, races in claim acceptance and bounty creation, and admin actions that bypassed the state rules.
- Production refuses to start with missing or unsafe settings.

---

## [1.0.0] - 2026-11-14

### Launch Release
- **Production Documentation**: Complete README with value proposition, browser-captured screenshots, 5-command local setup, deployment guides for Railway and Render, environment variable catalog, and full 6-Unit Django Syllabus Map.
- **Verification**: 51 automated tests passing with 88% project-wide test coverage and zero N+1 queries.
- **Live Readiness**: Deployment artifacts (`Procfile`, `release.sh`) verified with `python manage.py check --deploy`. (`render.yaml` was listed here but did not exist; it was added afterwards, see Unreleased.)

---

## [0.9.0] - 2026-11-14

### Trust, Moderation & Launch Hardening
- **Campus Moderation Queue**: Dedicated `/bounties/mod/` dashboard and inline tools for users with `bounties.moderate` permission or staff status to inspect abuse reports and toggle listing visibility.
- **Abuse Reporting**: Student reporting workflow on every public listing with reasons (`COMMERCIAL_SPAM`, `ACADEMIC_DISHONESTY`, `SCAM`, `HARASSMENT`, `OTHER`).
- **Rate Limiting**: Integrated `django-ratelimit` with `RatelimitMiddleware` across signup (10/min), login (15/min), bounty creation (10/hr), claim pitching (20/hr), and abuse reporting (10/hr).
- **SEO & Discovery**: Dynamic XML sitemap (`/sitemap.xml`) indexing active public listings and informational pages; search-engine-ready `/robots.txt`.
- **Infrastructure Health**: Plaintext monitoring endpoint at `/healthz` returning 200 OK.
- **Social Metadata**: OpenGraph and Twitter Cards metadata with custom 1200x630 neo-brutalist social preview asset (`static/img/og-card.png`).
- **Production Hardening**: Enforced SSL redirects, HSTS preload, secure CSRF and session cookies, and WhiteNoise compressed manifest static files storage.

---

## [0.6.0] - 2026-11-14

### Sessions & Cookies
- **Recently Viewed Strip**: Django session-backed tracking of last 5 unique viewed bounties rendered in a sticky ticker strip on the public board in Most-Recently-Used (MRU) order.
- **Layout Cookie**: Server-side `bb_layout` cookie (`grid` | `list`) with zero-JS `<form>` toggle buttons, persisting user presentation preference for 30 days.
- **User Dashboard**: Polished `/me/` view showcasing active open count (enforcing max 5 cap), claimed tasks, and submitted proposals.

---

## [0.5.0] - 2026-11-14

### Claims Lifecycle & Permissions
- **Atomic Acceptance**: Concurrency-locked claim acceptance using `transaction.atomic()` and `select_for_update()` on both claim and bounty records to prevent double-accept race conditions.
- **Single Solver Rule**: Accepting a claim automatically transitions the bounty to `CLAIMED`, confirms the accepted claimant, and safely rejects all other pending claims.
- **Contact Privacy**: WhatsApp/email contact info remains strictly private between poster and applicant; poster email is revealed only after claim acceptance.
- **Withdrawal**: Claimants can cleanly withdraw pending proposals prior to acceptance.
- **Email Notifications**: Console-based email notifications dispatched on claim submission, acceptance, and rejection via transactional `on_commit` hooks.

---

## [0.4.0] - 2026-11-14

### Bounties Domain Model & Exploration
- **Domain Modeling**: `Bounty`, `Claim`, and `Report` entities with clean validation rules (titles 8–80 chars, descriptions 20–1000 chars, rewards ₹10–₹10,000, future deadlines).
- **Filtering & Search**: Live board with full-text search (`?q=`), category filtering, status pills, and sorting (`newest`, `reward_high`, `reward_low`, `deadline`).
- **Performance**: Enforced 12-item pagination with `select_related("poster")` and `annotate(claim_count=Count("claims"))` to prevent N+1 queries.
- **Custom Template Tags**: `inr_format` for Indian currency grouping and `time_left` for humanized relative deadlines.
- **Realistic Demo Seeding**: Custom management command `python manage.py seed_demo` generating 7 campus users, 25 realistic non-lorem university bounties, and realistic claim threads.

---

## [0.3.0] - 2026-11-14

### Accounts & Authentication
- **User Registration**: Custom signup flow enforcing unique, case-insensitive emails with optional institutional domain restriction (`ALLOWED_EMAIL_DOMAIN`).
- **Authentication**: Flexible login with username or email, secure POST-only logout, and full password reset flow.
- **Role-Based Access Control**: Initial data migration creating the `"Moderators"` auth group equipped with the `bounties.moderate` permission.

---

## [0.2.0] - 2026-11-14

### Hand-Crafted Neo-Brutalist Design System
- **CSS Architecture**: Split CSS cascade across `tokens.css`, `components.css`, and `pages.css`.
- **Palette**: Cream `#FFF6DC`, ink `#111111`, flat yellow `#FFD60A`, pink `#FF5FA2`, mint `#5CE1A8`.
- **Visual Signature**: 3px solid ink borders, 0px border radius, 6px hard drop shadows (`8px` on hover, `0px` on active press), and -3deg tilted reward stickers.
- **Typography**: Display typography powered by Bricolage Grotesque and clean body text in Archivo.
- **Accessible & Resilient**: Accessible skip-links, WCAG AA contrast compliance, visible focus outlines, and full accessibility with JavaScript disabled.
- **Custom Error Handlers**: Bespoke neo-brutalist error templates for HTTP 400, 403, 404, and 500 status codes.
- **Interactive Styleguide**: Debug-only `/styleguide/` living catalog showcasing all design system components.

---

## [0.1.0] - 2026-11-14

### Foundation
- **Repository Setup**: Initialized Git repository with clean branching, MIT license, and automated GitHub Actions CI workflow.
- **Environment**: Configured Python 3.13 and Django 5.2 LTS with split settings (`base.py`, `dev.py`, `prod.py`).
- **Dependencies**: Streamlined production and developer requirements (`dj-database-url`, `whitenoise`, `psycopg`, `django-ratelimit`, `django-environ`, `gunicorn`, `django-debug-toolbar`).
