# Tests need PostgreSQL: export DATABASE_URL=postgres://localhost/bountyboard (SQLite hides real races).
PYTHON ?= python
SETTINGS = --settings=config.settings.test

.PHONY: install lock test coverage stress mutation

install:
	pip install --require-hashes -r requirements-dev.lock

# Re-resolve the hashed lockfiles after editing requirements*.txt. Set UPGRADE=--upgrade to bump versions.
lock:
	uv pip compile requirements.txt --universal --python-version 3.12 --generate-hashes $(UPGRADE) -o requirements.lock
	uv pip compile requirements-dev.txt --universal --python-version 3.12 --generate-hashes $(UPGRADE) -o requirements-dev.lock

test:
	$(PYTHON) manage.py test $(SETTINGS) --noinput

coverage:
	coverage run --branch manage.py test $(SETTINGS) --noinput
	coverage report --fail-under=91 --omit="*/tests*.py,*/migrations/*,manage.py,config/*"

# Run the concurrency tests 20 times; stops at the first failure.
stress:
	for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do \
	  $(PYTHON) manage.py test apps.bounties.tests_races apps.bounties.tests_race_anomalies \
	    apps.bounties.tests_accept_gating $(SETTINGS) --noinput || exit 1; \
	done

# Strip each lock, constraint and rate limit and require the tests to fail.
mutation:
	$(PYTHON) scripts/mutation_check.py
