---
inclusion: always
---

# Bnaoao Update Archive Context

- User/repository owner: `willbedoneuw`.
- This repository (`willbedoneuw/Meowv3`) is the persistent archive and session handoff for the next Bnaoao update.
- The implementation target is `willbedoneuw/Bnaoao`; canonical deployment branch is `production`.
- Before any implementation, read `archive/BNAOAO_UPDATE_PLAN_FA.md` completely.
- The approved portal UI is `archive/portal_ui_final.html`; its canonical SHA-256 is `52f8671d9a21509059cb2f6a619387b08fd3b71e7ffb110fcc5fcd361d3d7d55`.
- Preserve the approved UI's phone/password/code/success flow. Do not rewrite its working JavaScript unless the user explicitly requests it.
- Changes to inherited Bnaoao code must be strictly additive and isolated. Never rewrite core connection/session files.
- Keep `account_conn.py`, `rubika_client.py`, `worker.py`, `db.py`, and `main.py` byte-identical whenever possible; verify hashes before and after.
- Enforce one live connection per session and one running instance per service.
- Never push directly or force-push to `production`; use a new branch and PR.
- Before deployment: back up runtime data and `.env`, record rollback commit, run failure simulations and syntax checks, clear stale `__pycache__`, then restart exactly one service instance.
- Do not implement, commit, push, merge, or deploy merely because this archive exists. Wait for explicit user approval to start.
- Brain feature ideas are not part of the archived update unless the user explicitly adds them again.
- Communicate in concise Persian. Avoid unrelated ideas and avoid repeating features already present in the project.
