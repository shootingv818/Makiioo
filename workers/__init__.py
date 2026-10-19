"""Distributed master ↔ worker subsystem.

* ``worker``           master-side control: provision/update worker servers
                       over SSH, health checks, API tunnel, pick_worker_*.
* ``worker_api``       worker-side FastAPI node (loopback-only + Bearer).
                       Exposes /health, /login/*, /channel/*, /send/*,
                       /contacts/add, /gen/*, /broadcast/run, /logincode/*.
* ``worker_transfer``  isolated selection logic for "move send to another
                       worker" (excludes every previously-tried worker).
"""
