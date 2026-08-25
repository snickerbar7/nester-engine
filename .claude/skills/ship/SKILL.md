---
name: ship
description: Ship work to production in either Harriet Nester repo. Fire when the user says "ship", "push", "deploy", or wants local changes live. Single-branch policy on BOTH repos - any push to main IS a production deploy. Runs the right gate per repo and refuses to push red.
---

# Ship — push = production, both repos

No staging anywhere. Two repos, one rule each way:

| Repo | Deploys to | Deploy time | Gate |
|---|---|---|---|
| ENGINE — this one (~/Documents/Nester) | Render (Docker), serves Harriet AND the web product | ~3-5 min | `.venv/bin/python -m pytest -q` all green |
| WEB — ~/Documents/harriet-nester | Vercel, nester.harriet.com.mx | ~1 min | `npx eslint .` · `npm test` · `npm run build` · `npm run check:bundle` · `node scripts/check-docs.mjs` — its own `.claude/skills/ship/SKILL.md` has the full detail; read it when shipping there |

## Engine-specific gate points
1. Full pytest suite green. Never push red, never "just this once".
2. **Harriet compat is frozen**: anything under `service/harriet/` changed →
   the contract snapshot tests must still assert the exact envelope Harriet
   parses. Shape changes there need the user's explicit OK.
3. Docs in the same push: CLAUDE.md / docs/PRODUCT_PLAN.md when behavior,
   flags, env vars, or endpoints changed.
4. New env vars go into Doppler (`nester/prd`) BEFORE the deploy that reads
   them. Service auth: `NESTER_API_KEYS="client:token[:key_prefix]"`.

## Web-specific hard facts
- Git author MUST be onlinestuff628@gmail.com (repo-local config, already
  set) — Vercel BLOCKS deploys from other authors (status shows UNKNOWN
  forever). If a commit went in with the wrong author: `git commit --amend
  --reset-author` + force-push re-triggers.
- `vercel` CLI needs `--scope harriet1`; `vercel deploy` HANGS in sandboxes —
  ship by git push (Vercel builds from GitHub), or `vercel redeploy <url>
  --scope harriet1` for an existing build.
- New env vars into Vercel production BEFORE the push (NEXT_PUBLIC_* bake at
  build time). `vercel env ls production` to confirm.
- Schema changes: `node scripts/apply-schema.mjs` against Neon (additive,
  idempotent) before/with the deploy that reads them.

## After every push: verify in production
Deploy probe: background `until curl -s <route-marker> ...; do sleep 15; done`
on a route/behavior only the new build has. Then exercise the change like a
real user (login prueba-claude@example.com / prueba-secreta-123, drive
/api/chat SSE, download an artifact...). Report with evidence. If a live
finding appears, fix forward immediately — that is how most real bugs here
have been found.

## Never
- Push a red suite (a stale test-cache reading once caused a red push —
  always run the FULL suite fresh in the same command as the push).
- Let an agent push: agents commit, the main session gates and ships.
