---
name: ship
description: Ship the current work to production. Fire when the user says "ship", "push", "deploy", or wants local changes to go live. Single-branch policy - any push to main IS a production deploy (Render auto-deploys). Runs the full gate (tests, docs check, clean commit) and refuses to push red.
---

# Ship — push = production

This repo has NO staging. One branch (`main`): local = dev/experiments, any
push = production deploy of the Render service that **Harriet and Harriet
Nester both call in production**. So the gate is local and strict.

## The gate (all must pass before pushing)

1. **Full suite green**: `.venv/bin/python -m pytest -q` — zero failures.
   Never push red, never skip "just this once". If the user insists on
   pushing with a red suite, say what's red and that it deploys broken to
   Harriet's production, and require them to reaffirm explicitly.
2. **Contract safety**: if anything under `service/harriet/` changed, confirm
   the contract snapshot tests (`tests/test_service_contract.py`) still assert
   the exact envelope Harriet parses. Harriet compat is frozen — shape changes
   there need the user's explicit OK.
3. **Docs current**: if behavior, flags, env vars, or endpoints changed, update
   CLAUDE.md (and docs/PRODUCT_PLAN.md if product-relevant) IN THE SAME push.
4. **Clean tree**: everything intended is committed (`git status` clean);
   commit messages describe the change, not the process.

## Then

```bash
git push origin main
```

After pushing, remind the user (once, briefly) what just deployed and any env
vars Render/Doppler still needs (e.g. a new key in `NESTER_API_KEYS`).

## Env-var changes

New env vars are NOT picked up from code — they must be added in Doppler
(project for this service) before the deploy that reads them. Call this out
whenever a change introduces one.
