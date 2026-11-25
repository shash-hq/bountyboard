"""Settings for the test suite only (`--settings=config.settings.test`).

Identical to dev except for a fast password hasher. Django's default PBKDF2 is deliberately slow
(~0.2-1 s per hash on a laptop, seconds on the 0.1-CPU production instance) and the suite creates
thousands of users, so it dominated the run time. MD5 here is safe because no test database holds a
real credential. Never use this module with real credentials.

What is checked: `config/tests_settings.py` asserts base, dev and prod do not set PASSWORD_HASHERS,
and `config/tests_render.py` asserts render.yaml selects config.settings.prod. Neither stops someone
overriding DJANGO_SETTINGS_MODULE by hand on a deployment.
"""
from .dev import *  # noqa: F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
