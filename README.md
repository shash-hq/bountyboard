# Bounty Board 🎯

> Campus micro-task marketplace for Indian university students.

Post small bounties ("Debug my Arduino, ₹200"), claim tasks, settle offline.
Built with Python 3.12+, Django 5.2 LTS, server-rendered templates, and a handcrafted neo-brutalist design system.

## Quick Start (Dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

See [PROGRESS.md](PROGRESS.md) for milestone tracking.
