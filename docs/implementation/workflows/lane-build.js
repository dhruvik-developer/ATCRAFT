export const meta = {
  name: 'atcraft-lane-build',
  description: 'Build one round of ATCRAFT gap work packages in isolated lane worktrees (implement, then adversarial review-and-fix)',
  phases: [{ title: 'Build', detail: 'per lane: implement steps then review step, sequential within a lane, lanes in parallel' }],
}

const SPEC = 'C:/Project/Other/ATCRAFT/docs/implementation/specs'

const REPORT_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['complete', 'partial', 'blocked'] },
    summary: { type: 'string' },
    commits: { type: 'array', items: { type: 'object', properties: { sha: { type: 'string' }, subject: { type: 'string' } }, required: ['sha', 'subject'] } },
    migrations: { type: 'array', items: { type: 'object', properties: { revision: { type: 'string' }, down_revision: { type: 'string' }, purpose: { type: 'string' } }, required: ['revision', 'down_revision', 'purpose'] } },
    permissions_added: { type: 'array', items: { type: 'string' } },
    settings_added: { type: 'array', items: { type: 'string' } },
    report_keys_added: { type: 'array', items: { type: 'string' } },
    notification_types_added: { type: 'array', items: { type: 'string' } },
    tests: { type: 'array', items: { type: 'object', properties: { command: { type: 'string' }, result: { type: 'string' } }, required: ['command', 'result'] } },
    assumptions_added: { type: 'array', items: { type: 'string' } },
    known_gaps: { type: 'array', items: { type: 'string' } },
    integrator_notes: { type: 'array', items: { type: 'string' } },
  },
  required: ['status', 'summary', 'commits', 'tests', 'known_gaps', 'integrator_notes'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    ...REPORT_SCHEMA.properties,
    findings: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
      description: { type: 'string' },
      fixed: { type: 'boolean' },
    }, required: ['severity', 'description', 'fixed'] } },
  },
  required: ['status', 'summary', 'commits', 'tests', 'known_gaps', 'integrator_notes', 'findings'],
}

const TASKS = {
  full: `Implement the WHOLE work package (backend and frontend) exactly as specified. Order: backend models + migrations + schemas + repository + service + router + permissions (incl. apps/web/src/lib/permissions.ts parity) + presets + grant migration + demo data + backend tests; then regenerate the contract (openapi.json, src/types/openapi.ts, postman, docs/api.md count); then the frontend (types via SchemaOf, hooks, pages/tabs/dialogs, route-loaders/router/navigation/sweeps/handbook if a page is added, STATUS_VARIANTS) + frontend tests + npm run build; then docs. Commit in logical commits as you complete verified parts.`,
  backend: `Implement the BACKEND part of the work package: models, migrations (+ grant migration, + CHECK-swap if you add enum values used in a SQL CHECK), schemas, repository, service, router, permissions (enum, groups, presets AND the apps/web/src/lib/permissions.ts entries, because the parity test requires it), settings, Celery wiring, demo data, backend docs (database.md, assumptions.md placeholders, api.md, security.md as applicable) and backend tests (unit + integration + api). Run mypy and the full unit suite before committing. Finish by regenerating the contract (openapi.json, apps/web/src/types/openapi.ts, postman_collection.json) and fixing the docs/api.md request/folder count, so the frontend step can use generated types. Make sure apps/web typecheck still passes after the contract regeneration (fix any frontend compile breaks your API change causes). Do NOT build the new screens; a frontend agent follows you. Commit.`,
  resume: `TAKE OVER interrupted work. Another agent (Codex) started the work package(s) named above in this lane and was stopped by a usage limit before committing: its changes are UNCOMMITTED in the lane worktree. Follow BRIEF section 1a exactly: inventory every uncommitted change and untracked file against each named spec (decisions + test list) before editing; keep what is right, fix what is wrong, finish what is missing; delete scratch files you have confirmed are scratch; never discard work wholesale. The finished result must be the WHOLE of each named package as the 'full' task describes: backend (models, migrations incl. grant/CHECK-swap, schemas, repository, service, router, permissions + lib/permissions.ts parity, presets, demo data), regenerated contract and docs/api.md count, frontend (screens, dialogs, hooks, routes/navigation/sweeps/handbook, STATUS_VARIANTS, responsive), docs (database.md, assumptions placeholders, security/architecture as applicable) and tests (unit, integration, api, frontend). Verify everything yourself on the final tree (the previous agent's claims are not verification), including the lane live check from the brief. Commit in logical commits (one per package where the changes separate cleanly) and end with a clean git status.`,
  frontend: `Implement the FRONTEND part of the work package on top of the backend already committed in this lane by a previous agent (run git log to see it; read the generated types in apps/web/src/types/openapi.ts and the backend routers you call). Screens, tabs, dialogs, hooks, query keys, route-loaders/router/navigation, e2e sweep script entries, capture-screens + handbook content for new pages, STATUS_VARIANTS, lib/messages.ts and lib/constants.ts entries, responsive layout (phone/tablet/desktop), frontend tests with the renderScreen harness, then npm run typecheck, lint, format:check, targeted vitest, npm run build (bundle budget). If you find a backend gap that blocks the UI, fix it minimally in the backend with a test, and regenerate the contract. Commit.`,
}

function implPrompt(lane, step, args) {
  return `You are an implementation agent for the ATCRAFT gap build, round ${args.round}, lane ${lane.key} (${lane.name}).
Your lane worktree root is ${lane.root} (branch ${lane.branch}). Edit ONLY files under that root, using absolute paths. Every shell command must cd into the lane first.

1. Read ${SPEC}/BRIEF.md completely. It is binding (environment, commands, rules, commit protocol, return format).
2. Read your work-package spec(s) completely: ${(step.wps || [step.wp]).map(w => `${SPEC}/${w}.md`).join(' and ')}. Then read the background report sections they name (in the reports folder named in the brief).
3. Before writing code, read the existing code you will change and the precedent files the spec/report names; follow the codebase's existing conventions exactly.
4. TASK: ${TASKS[step.kind]}
5. Verify honestly (exact commands, exact results). Commit per the brief. End with a clean git status in the lane.
6. Return the structured report (status, summary, commits, migrations, permissions/settings/report keys/notification types added, tests with exact results, assumption placeholders, known gaps, integrator notes).
Main was at ${args.base_sha} when this round started; earlier commits in your lane since then belong to previous steps of this round.`
}

function reviewPrompt(lane, step, args) {
  return `You are the adversarial REVIEWER-FIXER for work package ${step.wp}, round ${args.round}, lane ${lane.key} (${lane.name}).
Lane worktree root: ${lane.root} (branch ${lane.branch}). Edit ONLY files under that root (absolute paths; cd into the lane in every shell command).

1. Read ${SPEC}/BRIEF.md and ${SPEC}/${step.wp}.md completely, and the report sections the spec names.
2. Inspect the work: git -C ${lane.root} log --oneline ${args.base_sha}..HEAD and git -C ${lane.root} diff ${args.base_sha}..HEAD (read every changed file in full where it matters). Part of it may have been written by another agent (Codex) and taken over by a later one; review all of it equally hard, and focus on the files and behaviour of ${step.wp} (other packages in the same diff have their own reviewers).
3. Hunt for defects and gaps, assuming they exist. Check at least:
   - Every numbered decision in the spec is implemented as written (list any deviation).
   - CLAUDE.md non-negotiables and every listed trap: thin routers; tenant scope; a UUID FK is never trusted (tenant + kind/active checks on every incoming id, on create AND update); audit rows in the same transaction; status changes only via StateMachine.assert_transition + record_status_change; Decimal money; every list paginated; no await on the DB inside loops; lazy="raise" and the model_validate-reads-relationships trap; order by (created_at, id) on audit/ledger; no 'clause or default'; empty-list IN filters return nothing; partial unique indexes demote-before-promote; never session.rollback() in a service; Python-side onupdate only.
   - Migrations: alembic heads == 1; upgrade head / downgrade -1 / upgrade head on the lane DB succeed; review docstring present; literal values; CHECK names <= 63 bytes; NOT VALID + VALIDATE for FKs on big tables; grant migration for new permissions; CHECK-swap for new enum values in SQL CHECKs (ReportKey list must contain ALL current keys).
   - Permissions: enum + PERMISSION_GROUPS + presets + lib/permissions.ts + literal Permission.X in decorators + UI gates on the same right; vendor/dealer isolation where relevant.
   - Frontend: SchemaOf types (or registered mirrors), STATUS_VARIANTS for new statuses, sortable columns only if whitelisted, catalogue constants/messages (no literal limits/debounce), responsive layout, route-loaders/router/navigation/four sweep scripts for new pages, handbook entries, gates match endpoint permissions, buttons only on states the machine can leave.
   - Docs: database.md, assumptions placeholders (§WP-n), api.md count + map, security.md, handbook.
   - Demo data idempotent (run python -m src.core.demo_data twice on the lane DB).
   - Tests exist for the spec's test list and actually assert behaviour; add missing ones.
4. FIX everything you find (code, tests, docs). Then run the verification set and report exact results:
   backend: ruff check + ruff format --check; mypy src; the FULL unit suite; every integration/api test file this package added or changed plus direct neighbours; alembic heads/upgrade/downgrade/upgrade; contract regenerated and up to date (python -m scripts.export_openapi --check; npm run contract:check; python -m scripts.export_postman --check) and docs/api.md count correct;
   frontend: npm run typecheck, npm run lint, npm run format:check, vitest on changed/added test files, npm run build.
5. Commit your fixes per the brief. End with a clean git status.
6. Return the structured report including findings[] (severity, description, fixed). Be truthful about anything still failing.
Main was at ${args.base_sha} when this round started.`
}

phase('Build')
const results = await parallel(args.lanes.map(lane => async () => {
  const out = []
  for (const step of lane.steps) {
    const isReview = step.kind === 'review'
    const prompt = isReview ? reviewPrompt(lane, step, args) : implPrompt(lane, step, args)
    const wpName = (step.wps || [step.wp]).join('+')
    log(`lane ${lane.key}: starting ${wpName} ${step.kind}`)
    const r = await agent(prompt, {
      label: `${lane.key}:${(step.wps || [step.wp]).join('+')}:${step.kind}`,
      phase: 'Build',
      schema: isReview ? REVIEW_SCHEMA : REPORT_SCHEMA,
    })
    out.push({ step: `${wpName}:${step.kind}`, result: r })
    if (!r) { log(`lane ${lane.key}: ${wpName} ${step.kind} returned nothing; stopping lane`); break }
    log(`lane ${lane.key}: ${wpName} ${step.kind} -> ${r.status}`)
    if (r.status === 'blocked') break
  }
  return { lane: lane.key, out }
}))
return results
