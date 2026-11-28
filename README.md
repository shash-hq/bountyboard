#  Bounty Board

> **Campus micro-task marketplace for Indian university students.**  
> Post small bounties (*"Debug my Arduino, ₹200"*, *"Format my LaTeX lab report, ₹150"*), claim them, get selected atomically, do the work offline, and get paid directly in cash or UPI. Zero platform fees. Zero middlemen.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Django 5.2 LTS](https://img.shields.io/badge/django-5.2%20LTS-092E20.svg)](https://www.djangoproject.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests Passing](https://img.shields.io/badge/tests-51%20passed-5CE1A8.svg)](apps/bounties/tests.py)
[![Test Coverage](https://img.shields.io/badge/coverage-88%25-FFD60A.svg)](apps/bounties/tests.py)
[![Style: Neo-Brutalist](https://img.shields.io/badge/style-neo--brutalism-FF5FA2.svg)](static/css/tokens.css)

---

## Screenshots

| Desktop Live Board (`1280px`) | Mobile View (`375px`) |
| :---: | :---: |
| ![Desktop Live Board](docs/screenshots/board-desktop.png) | ![Mobile Live Board](docs/screenshots/mobile-board.png) |

| Bounty Detail & Pitch Form | Neo-Brutalist Component Styleguide |
| :---: | :---: |
| ![Bounty Detail Page](docs/screenshots/bounty-detail.png) | ![Styleguide](docs/screenshots/styleguide.png) |

---

##  Live Demo & Deployment

- **Live URL**: [https://bountyboard.up.railway.app](https://bountyboard.up.railway.app) (Staging: [https://bountyboard-demo.onrender.com](https://bountyboard-demo.onrender.com))
- **Demo Accounts**:
  - Moderator: `campus_mod` / `Password123!` (holds `bounties.moderate` permission)
  - Student: `priya_cs` / `Password123!`
  - Student: `rohit_ee` / `Password123!`

---

##  Run Locally in 5 Commands

```bash
git clone https://github.com/shash-hq/bountyboard.git && cd bountyboard
python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock
python manage.py migrate && python manage.py seed_demo
python manage.py runserver
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. (To inspect all design system components, visit [http://127.0.0.1:8000/styleguide/](http://127.0.0.1:8000/styleguide/)).

---

##  Product Philosophy & Rules

1. **Settle Offline**: In v1, there are no payment gateways or real-time chat rooms. The UI clearly states **"Settle Offline via Cash or Direct UPI"**. Work and verification happen face-to-face on campus.
2. **Atomic Single Solver**: When the poster accepts one claim, a database row-lock (`select_for_update`) executes inside `transaction.atomic()`. The chosen claim becomes `ACCEPTED`, other pending claims are automatically `REJECTED`, and the bounty status switches to `CLAIMED`.
3. **Contact Privacy**: WhatsApp/email contacts entered during claims are visible **only to the bounty poster**. The poster's contact email is revealed to the claimant only after their claim is accepted.
4. **Campus Discipline**:
   - Students may have at most **5 active OPEN bounties** at any time.
   - Claims can only be filed on open, non-expired listings, and students cannot claim their own bounties.
   - Claimants can cleanly withdraw pending claims before poster review.
   - Campus moderators (in the `Moderators` group) review flagged abuse reports and can hide listings from the board.

---

##  Hand-Crafted Neo-Brutalist Design System

Bounty Board features an authentic, hand-coded neo-brutalist aesthetic with zero bloated front-end frameworks (no React, Tailwind, or Bootstrap):

- **Palette**:
  - Cream Background: `var(--cream)` (`#FFF6DC`)
  - Deep Ink: `var(--ink)` (`#111111`)
  - Accent Yellow: `var(--yellow)` (`#FFD60A`)
  - Punch Pink: `var(--pink)` (`#FF5FA2`)
  - Mint Green: `var(--mint)` (`#5CE1A8`)
  - Clean White: `var(--white)` (`#FFFFFF`)
- **Borders & Shadows**: Every container, card, and input features a `3px solid var(--ink)` border with a `0px` border radius and a hard `6px 6px 0 var(--ink)` drop shadow (expands to `8px` on hover; translates `4px 4px` with `0px` shadow when pressed active).
- **Typography**: Fluid headlines in **Bricolage Grotesque** (display 800) paired with legible body text in **Archivo**.
- **Accents**: Bounty rewards are tilted stickers (`-3deg`), and statuses are rendered as authentic stamped badges.
- **Zero-JS Resilient**: 100% of user flows (filtering, searching, posting, claiming, layout switching, moderation) function identically with JavaScript turned off.

---

##  Django Syllabus Map

This repository serves as a reference implementation mapping every core concept in modern server-rendered Django back to production code:

| Unit | Topic / Concept | Implementation File / URL | Description |
| :--- | :--- | :--- | :--- |
| **Unit I** | Project & App Structure | [`config/settings/`](file:///Users/shashankranjan/Developer/bountyboard/config/settings/base.py), [`apps/`](file:///Users/shashankranjan/Developer/bountyboard/apps/) | Split settings (`base.py`, `dev.py`, `prod.py`), decoupled local apps (`accounts`, `bounties`, `pages`). |
| **Unit I** | Custom Management Commands | [`apps/bounties/management/commands/seed_demo.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/management/commands/seed_demo.py) | `python manage.py seed_demo` populates realistic non-lorem campus data with users and claims. |
| **Unit II** | Views & HTTP Methods | [`apps/bounties/views.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/views.py) | View functions enforcing strict `@require_http_methods(["GET", "POST"])` and `@require_POST`. |
| **Unit II** | URL Routing & Path Converters | [`apps/bounties/urls.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/urls.py), [`config/urls.py`](file:///Users/shashankranjan/Developer/bountyboard/config/urls.py) | Namespaced routes with `<int:pk>` and `<slug:slug>` converters; strict route precedence. |
| **Unit II** | Custom Error Handlers | [`apps/pages/views.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/pages/views.py#L68-L81) | Custom handlers for HTTP 400, 403, 404, and 500 error pages with neo-brutalist templates. |
| **Unit III** | Template Inheritance & Layout | [`templates/base.html`](file:///Users/shashankranjan/Developer/bountyboard/templates/base.html) | Root layout with accessible skip link, announcement ticker, responsive nav, and message blocks. |
| **Unit III** | Custom Template Tags & Filters | [`apps/bounties/templatetags/bounty_tags.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/templatetags/bounty_tags.py) | Filters `inr_format` (Indian numbering system) and `time_left` (humanized relative deadlines). |
| **Unit III** | Debugging & Automated Testing | [`apps/bounties/tests.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/tests.py) | Django Debug Toolbar integration in dev; 51 unit & transaction tests verifying concurrency and denials. |
| **Unit IV** | Forms & Post-Redirect-Get (PRG) | [`apps/bounties/forms.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/forms.py) | ModelForms with field cleaning, CSRF verification, and PRG with `django.contrib.messages`. |
| **Unit IV** | Form Validation & Clean Methods | [`apps/bounties/forms.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/forms.py#L32-L48), [`apps/accounts/forms.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/accounts/forms.py#L22-L35) | `clean_email`, `clean_reward`, `clean_deadline`, and institutional domain validation. |
| **Unit V** | Models, ForeignKeys & Constraints | [`apps/bounties/models.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/models.py) | `Bounty`, `Claim`, and `Report` models with `ForeignKey`, `OneToOneField`, unique constraints, and custom properties. |
| **Unit V** | Migrations & Data Migrations | [`apps/accounts/migrations/0001_initial.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/accounts/migrations/0001_initial.py) | Data migration creating `"Moderators"` group and assigning the `bounties.moderate` permission. |
| **Unit V** | Django Admin & Bulk Actions | [`apps/bounties/admin.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/admin.py) | Custom admin classes with `list_display`, `list_filter`, search fields, and bulk moderation actions. |
| **Unit V** | Transactions & Concurrency | [`apps/bounties/services.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/services.py#L11-L69) | `transaction.atomic()` + `select_for_update()` guaranteeing race-free claim acceptance. |
| **Unit VI** | Cookies & Layout Preference | [`apps/pages/views.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/pages/views.py#L55-L65) | Server-side `bb_layout` cookie (`grid` \| `list`) updated via zero-JS form POST. |
| **Unit VI** | Sessions & Recent History | [`apps/bounties/views.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/bounties/views.py#L106-L113) | `request.session['recently_viewed_bounties']` tracking last 5 viewed items in MRU order. |
| **Unit VI** | Authentication & Guards | [`apps/accounts/views.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/accounts/views.py), [`apps/accounts/urls.py`](file:///Users/shashankranjan/Developer/bountyboard/apps/accounts/urls.py) | Custom signup, login (email or username), logout, password reset flow, and `@login_required(next=...)`. |

---

##  Environment Configuration

Configuration is managed via `django-environ`. Copy `.env.example` to `.env` to configure your local or production instance:

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `SECRET_KEY` | String | *Dev only; **required in prod*** | Django secret key. Production refuses to boot without one (50+ chars, not a known/insecure value). |
| `DEBUG` | Boolean | `True` in dev, `False` in prod | Enables Django debug mode and debug toolbar. |
| `DATABASE_URL` | String | `sqlite:///db.sqlite3` | Database connection URL (e.g., `postgres://user:pass@host:5432/db`). |
| `ALLOWED_HOSTS` | List | `localhost,127.0.0.1` in dev; **required in prod** | Comma-separated allowed HTTP host headers. No `*` in production. |
| `CSRF_TRUSTED_ORIGINS` | List | *None*; **required in prod** | Comma-separated explicit `https://` origins for CSRF validation (no wildcards). |
| `TRUSTED_PROXY_COUNT` | Integer | `0` | Number of trusted reverse proxies that append to `X-Forwarded-For`. `0` ignores the header (rate limits then see the proxy's IP). Set it only after checking the real deployment. |
| `SITE_URL` | String | `http://localhost:8000` in dev; **required in prod** | Public `https://` base URL of the site. |
| `ALLOWED_CAMPUS_DOMAINS` | List | `lpu.co.in` | Email domains (exact match) that make a confirmed address campus-verified. Signup itself is open to any domain. |
| `EMAIL_URL` | String | `consolemail://` | Email backend URL (`smtp+tls://user:pass@smtp.example.com:2525` in production). Unset in production, emails are only printed to the logs. |
| `SECURE_SSL_REDIRECT` | Boolean | `True` in prod | Forces HTTPS redirects on all incoming connections. |
| `SESSION_COOKIE_SECURE` | Boolean | `True` in prod | Restricts session cookies to HTTPS connections only. |
| `CSRF_COOKIE_SECURE` | Boolean | `True` in prod | Restricts CSRF cookies to HTTPS connections only. |

---

##  Deployment (Railway & Render)

The project ships ready-to-deploy with Gunicorn and WhiteNoise static asset compression.

### Deploying to Railway
1. Fork or clone this repository to your GitHub account.
2. In Railway, click **New Project &rarr; Deploy from GitHub repo**.
3. Add a PostgreSQL database to the project.
4. Set the following Environment Variables in Railway:
   - `DJANGO_SETTINGS_MODULE=config.settings.prod`
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   - `SECRET_KEY=<generate-a-secure-random-key>`
   - `ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}}`
   - `CSRF_TRUSTED_ORIGINS=https://${{RAILWAY_PUBLIC_DOMAIN}}`
5. Railway uses the included [`Procfile`](Procfile) to start Gunicorn and automatically runs migrations and static collection on release.
6. Do **not** run `seed_demo` on a deployment: it is for local development and refuses to run when `DEBUG` is off. A real campus database starts empty.

### Deploying to Render
Target: a free Render web service in Singapore with a free Supabase Postgres (also Singapore).
1. In Supabase, create a project and copy the **Supavisor session-mode pooler** connection string
   (port 5432). The direct connection is IPv6-only and will not work from Render.
2. In Render, click **New &rarr; Blueprint** and select this repository. [`render.yaml`](render.yaml)
   defines the service; Render asks once for the values marked `sync: false`:
   `SECRET_KEY`, `DATABASE_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SITE_URL`,
   `REG_NO_HMAC_KEY`, `CARD_NAME_KEYS`, `TICK_SECRET`, `EMAIL_URL` and `DEFAULT_FROM_EMAIL`.
   The comments in `render.yaml` say how to generate the secrets. Render's free tier blocks SMTP
   ports 25, 465 and 587, so `EMAIL_URL` uses 2525. Production **refuses to boot** if a required
   value is missing or unsafe (see the environment table above).
3. Build runs `collectstatic` and `migrate`, so a failing migration fails the deploy and the
   previous version keeps serving. Gunicorn runs with 1 worker and 4 threads.
4. `TRUSTED_PROXY_COUNT` is left unset on purpose; check the real `X-Forwarded-For` chain on the
   live instance before setting it (rate limits see the proxy address until then).

Tests check `render.yaml` against `prod.py`; they cannot run a deploy, so verify and measure each
new deployment by hand.

---

##  Testing & Quality Assurance

Run the test suite with coverage:

```bash
coverage run manage.py test
coverage report
```

### Coverage Summary
- **Total Tests**: 51 passed (0 failures, 0 errors).
- **Project Coverage**: 88%.
- **Zero N+1 Queries**: Verified on live board, bounty detail, and user dashboard.
- **Race Condition Prevention**: Concurrency tests verify that multiple simultaneous claims cannot be accepted for the same bounty.

---

##  License

Distributed under the MIT License. See [`LICENSE`](LICENSE) for details.
