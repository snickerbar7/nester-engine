---
name: design-round
description: Run one round of the Claude Design -> code loop for Harriet Nester. Fire when the user says a design round is done, drops a handoff folder path (usually /Users/marlontag/Downloads/design_handoff_*), or asks to implement canvas changes. Covers diffing, staging, agent briefs, gates, promotion, and shipping across both repos.
---

# The canvas -> code loop (Harriet Nester)

Two repos: ENGINE = this one (~/Documents/Nester, Python, Render, push=prod
deploy) and WEB = ~/Documents/harriet-nester (Next.js, Vercel, push=prod).
The design canvas is a claude.ai/design project; the user exports a HANDOFF
folder. `design/` in the WEB repo is always "what the app implements".

## The loop, per round
1. DIFF the handoff against ~/Documents/harriet-nester/design/ (normalize:
   `diff <(tr -d ' \n' < new) <(tr -d ' \n' < old)`). NEW + CHANGED files =
   the work list. Read the handoff README.md — since round 6 it carries an
   implementation-state table and behavior specs.
2. STAGE: copy the handoff to ~/Documents/harriet-nester/design-next/
   (only when no agent is mid-flight in that repo — NEVER two agents in one
   working tree; queue rounds instead, collisions have bitten twice).
3. CONTRACT FIRST when the round implies engine capability: define the /v1
   request/response delta, build it in the ENGINE repo (separate agent, can
   run parallel — different repo), ship it, THEN the web round consumes it.
4. IMPLEMENT via an opus agent in the WEB repo. Brief must include: diff
   design-next vs design first; SSOT (src/lib/product.ts) for every
   price/plan/limit/contact; i18n both catalogs (es canonical, shop
   register); tone = foreman (no greetings/exclamations); the iron rule (no
   invented numbers - missing data renders em dash); money line (cards only
   escort to Stripe, never charge); docs gate (docs/MAP.json + check-docs);
   commit per milestone, never push; promote design-next -> design at the
   end and delete design-next.
5. GATE + SHIP (the main session, never the agent): eslint, npm test,
   build, check:bundle, check-docs -> push (= production). Engine repo:
   pytest -> push (= Render deploy).
6. VERIFY IN PROD like a user: curl/SSE against https://nester.harriet.com.mx
   (login POST /api/auth/login, drive /api/chat with SSE, poll deploys with
   a background until-loop probe). Report with evidence, fix live findings
   immediately (several prod bugs were only visible through the real AI path).

## Facts that save an hour
- Both repos: any push to main IS a production deploy; gates are local+CI.
- Web repo git author must stay onlinestuff628@gmail.com (Vercel blocks
  other authors); `vercel` CLI needs --scope harriet1; `vercel deploy`
  hangs in sandboxes — push-to-deploy or `vercel redeploy <url>` instead.
- Engine service auth: NESTER_API_KEYS client keys; the web client's key is
  in ~/Documents/harriet-nester/.env.local (NESTER_SERVICE_KEY).
- Test account: prueba-claude@example.com / prueba-secreta-123 (synthetic).
- Sheet solves are async (POST /v1/jobs); tube is sync on /v1/nest.
- The user's own account is marlontager7+owner@gmail.com — never touch it.
