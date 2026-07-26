---
inclusion: always
---

# Makiioo — Project Context

- Repository: `shootingv818/Makiioo`. Owner works in Persian; reply concisely in Persian.
- Base code = an exact copy of the latest `willbedoneuw/Meowv3` (branch
  `fix/tg-multi-send-robust-v1`, merged in `a6152c4`). Worker-provisioning fix
  was ported from `shootingv818/Haopooonwkkoo` (reference only).
- **Before any change, read `project_notes/MAKIIOO_MAP.md`** — it is the
  authoritative map (architecture, what changed in the 4-part update, session
  conflict notes, and the mandatory deploy note about `GIT_REPO_URL`).

## Working rules
- Do NOT rewrite the base connection/session/architecture logic. Keep changes
  additive and isolated; reuse the project's own code as much as possible and
  write new code only when unavoidable.
- Enforce one live connection per session and one running instance per service.
- Delete an account only on a confirmed-invalid session AND explicit owner
  confirmation (quarantine panel). Temporary errors (timeout, network, FloodWait,
  Worker unavailable) never delete/quarantine.
- Never push directly or force-push to `main`; use a new branch and a PR.
- Do not add test files; verify with `python -m compileall` + manual smoke checks.
- Always confirm with the owner before writing code for a NEW request.

## Already-done in this update (do not redo)
- Worker `provision_worker` / `update_worker` robustness fix (2 functions only).
- 🧠 Channel Brain replaced the old broadcaster UI.
- Rubika-send + Brain log cards converted to English (automation cards excluded).
- Account health engine merged with the portal Watcher (worker health loop kept
  separate). See MAKIIOO_MAP.md for exact scope and what was intentionally left.
