export const meta = {
  name: 'atcraft-integrate-round',
  description: 'Merge one round of ATCRAFT lane branches into main, reconcile, verify, migrate the dev DB and fast-forward the lanes',
  phases: [{ title: 'Integrate', detail: 'one integrator agent following INTEGRATOR.md' }],
}

const SPEC = 'C:/Project/Other/ATCRAFT/docs/implementation/specs'

const SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['complete', 'partial', 'blocked'] },
    summary: { type: 'string' },
    main_head: { type: 'string' },
    commits: { type: 'array', items: { type: 'object', properties: { sha: { type: 'string' }, subject: { type: 'string' } }, required: ['sha', 'subject'] } },
    conflicts: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, resolution: { type: 'string' } }, required: ['file', 'resolution'] } },
    migration_chain: { type: 'array', items: { type: 'string' } },
    assumption_renumbering: { type: 'array', items: { type: 'string' } },
    verification: { type: 'array', items: { type: 'object', properties: { command: { type: 'string' }, result: { type: 'string' } }, required: ['command', 'result'] } },
    dev_db: { type: 'string' },
    lanes_fast_forwarded: { type: 'array', items: { type: 'string' } },
    open_problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['status', 'summary', 'main_head', 'commits', 'conflicts', 'migration_chain', 'verification', 'dev_db', 'lanes_fast_forwarded', 'open_problems'],
}

phase('Integrate')
const r = await agent(`You are the INTEGRATOR for round ${args.round} of the ATCRAFT gap build.
Read ${SPEC}/INTEGRATOR.md completely and follow it exactly; read ${SPEC}/BRIEF.md for commands and rules (they apply to you with paths in the main tree C:/Project/Other/ATCRAFT).
Main HEAD before this round's merge: ${args.old_head}. Alembic head on main before this round: ${args.old_alembic_head}.
Lanes to merge in this order (skip a lane whose branch has no commits beyond main, but still fast-forward it afterwards): ${args.lanes.join(', ')}.
Notes from this round's lane agents (conflict hotspots, migration ids, deviations) — use them:
${args.notes}
Additional instructions for this round:
${args.extra || '(none)'}
Work carefully and completely: merges, reconciliation, contract regeneration, verification, dev DB migrate+seed+demo, lane fast-forward and lane DB re-clone. Fix integration breakage on main with clear commits. Be truthful in the report; never claim a check passed unless you saw it pass.`, { label: `integrate:r${args.round}`, phase: 'Integrate', schema: SCHEMA })
return r
