"""Immutable foundation layer (CORE_RULES.md).

Modules in this package must stay byte-identical whenever possible:

* ``account_conn``  single warm Rubika connection per account (session pool).
* ``db``            SQLite data layer: accounts, settings, workers, jobs, ...
* ``crypto_util``   Fernet encryption for SSH credentials and tokens at rest.

Never rewrite these modules; evolve the system in additive sibling packages
(``rubika/``, ``workers/``, ``bot_app/``, ``portal/``, ``status/``).
"""
