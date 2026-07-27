export const meta = {
  name: 'project-index',
  description: 'Index a repo via parallel read-only discovery agents + an adversarial verifier, then synthesize six evidence-backed primitive docs (REQ/CONTEXT/STATE/TDD/DESIGN/DECISIONS). Returns the doc contents; the caller writes them.',
  phases: [
    { title: 'Discover', detail: '3 read-only mappers (context / product+state / quality+design)' },
    { title: 'Verify', detail: 'one adversarial gate tries to disprove every claim' },
    { title: 'Synthesize', detail: 'integrate only survivors into six primitive docs' },
  ],
}

// args: { repo: "<absolute repo path>" }  (injected by workflow_job / Workflow args)
const REPO = (args && args.repo) || '.'

// Shared, read-only, evidence-labelled preamble (ported from Dale's index-roles, runtime-neutral).
const PREAMBLE = `You are ONE read-only discovery agent indexing a software repository.

Repository root: ${REPO}

Inspect the repo READ-ONLY. Do NOT edit files, create artifacts, change git state,
commit, stash, reset, push, install deps, or read secret VALUES. Do NOT spawn further
agents. Read applicable AGENTS.md / CLAUDE.md / README first.

Code, tests, schemas, and runtime output OUTRANK prose docs. Label every material claim
VERIFIED / INFERRED / UNKNOWN / STALE. For VERIFIED, give a source pointer (path +
line/symbol/test/exact non-secret command). Never turn absence of evidence into a
confident statement. Name required env vars, never their values.`

const DISCOVERY_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    coverage: { type: 'string', description: 'what you inspected and did not' },
    verified: { type: 'array', items: { type: 'string' } },
    inferences: { type: 'array', items: { type: 'string' } },
    contradictions: { type: 'array', items: { type: 'string' }, description: 'contradictions / stale docs' },
    unknowns: { type: 'array', items: { type: 'string' } },
    proposed_sections: { type: 'string', description: 'draft markdown sections for THIS role\'s owned primitive doc(s), evidence-backed' },
    risks: { type: 'array', items: { type: 'string' } },
  },
  required: ['coverage', 'verified', 'proposed_sections'],
}

const ROLES = [
  { key: 'context', label: 'discover:context', focus:
`ROLE: Context Cartographer -> owns CONTEXT.md.
Focus: runtime entrypoints & high-level architecture; modules & ownership boundaries;
language/frameworks/package manager/database/infra; public & internal contracts;
storage/security/perf/operational commands; repo map & generated-code boundaries;
the AGENTS.md/CLAUDE.md hierarchy. Draft evidence-backed CONTEXT.md sections. Suggest
durable architectural decisions for later verification but do NOT decide their status.` },
  { key: 'product', label: 'discover:product', focus:
`ROLE: Product & State Historian -> owns REQ.md and STATE.md.
Focus: product/library purpose & current users; observable jobs, scope, requirements,
acceptance criteria; active implementation state from code/tests/TODOs; recent completed
work from git history if present; blockers, risks, gaps, next actions grounded in
evidence; differences between intended and current behaviour. Draft REQ.md + STATE.md
sections. Keep aspirational roadmap SEPARATE from verified current state.` },
  { key: 'quality', label: 'discover:quality', focus:
`ROLE: Quality & Design Auditor -> owns TDD.md and DESIGN.md.
Focus: test runners/suites/fixtures/CI + exact local commands; gaps between
requirements/contracts and coverage; flakiness, env assumptions, quality gates; UI
surfaces, design tokens, shared primitives, layout rules, component states,
accessibility, responsiveness, motion, content conventions; whether DESIGN.md applies at
all. Draft TDD.md + DESIGN.md sections. Do NOT invent colours, tokens, components,
coverage targets, or CI behaviour. If there is no UI, say so with evidence.` },
]

phase('Discover')
const reports = await parallel(ROLES.map((r) => () =>
  agent(`${PREAMBLE}\n\n${r.focus}\n\nReturn the structured report.`,
    { label: r.label, phase: 'Discover', schema: DISCOVERY_SCHEMA })
))
const named = ROLES.map((r, i) => ({ role: r.key, report: reports[i] })).filter((x) => x.report)
if (!named.length) return { error: 'all discovery agents failed', docs: null }

phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    pass: { type: 'array', items: { type: 'string' }, description: 'claims safe to integrate, with evidence' },
    revise: { type: 'array', items: { type: 'string' }, description: 'need narrower wording / newer evidence' },
    reject: { type: 'array', items: { type: 'string' }, description: 'unsupported or false claims' },
    missing: { type: 'array', items: { type: 'string' }, description: 'material areas no report inspected' },
    decisions: { type: 'array', items: { type: 'string' }, description: 'choices demonstrably made: context, alternatives, consequences, evidence, proposed status' },
    conflicts: { type: 'array', items: { type: 'string' }, description: 'cross-document conflicts' },
    final_gate: { type: 'string', enum: ['PASS', 'FAIL'] },
    smallest_fix: { type: 'string', description: 'smallest action needed to pass' },
  },
  required: ['pass', 'reject', 'final_gate'],
}
const verifier = await agent(
`You are the ADVERSARIAL verification gate for a project-index run.

Repository root: ${REPO}
Raw discovery reports (JSON):
${JSON.stringify(named, null, 1).slice(0, 120000)}

Inspect the repo READ-ONLY. Do NOT edit files or git state, do NOT spawn agents. TRY TO
DISPROVE every important claim. Check evidence freshness, source quality, contradictions,
missing coupled layers, and fit to the six primitive contracts (REQ/CONTEXT/STATE/TDD/
DESIGN/DECISIONS). Do NOT accept a claim merely because multiple reports repeated it.
Return the structured verdict.`,
  { label: 'verify:adversarial', phase: 'Verify', schema: VERIFY_SCHEMA, effort: 'high' })

phase('Synthesize')
const OUT = `${REPO}/.project-index`
const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    written: { type: 'array', items: { type: 'string' }, description: 'absolute paths of the six files written' },
    unknowns: { type: 'array', items: { type: 'string' }, description: 'facts left explicitly UNKNOWN' },
    summary: { type: 'string', description: '3-5 line summary of what the index says + what the verifier rejected' },
  },
  required: ['written', 'summary'],
}
const docs = await agent(
`You are the INTEGRATOR for a project-index run. Produce six non-overlapping primitive docs
for repository ${REPO}, using ONLY claims that survived verification, and WRITE each one to
its file (mkdir -p ${OUT} first), then return the manifest:
  ${OUT}/REQ.md  ${OUT}/CONTEXT.md  ${OUT}/STATE.md  ${OUT}/TDD.md  ${OUT}/DESIGN.md  ${OUT}/DECISIONS.md

Discovery reports (JSON):
${JSON.stringify(named, null, 1).slice(0, 120000)}

Verifier verdict (JSON):
${JSON.stringify(verifier, null, 1).slice(0, 40000)}

Rules:
- Integrate ONLY survivors: pass-claims, and revise-claims with the verifier's narrower
  wording. NEVER integrate rejected claims. Mark unresolved conflicts/unknowns explicitly
  ("UNKNOWN: ..."); never fill a gap with a plausible guess.
- Ownership (do not duplicate a fact across docs; link to its owner instead):
  REQ = product intent/users/scope/requirements/acceptance criteria.
  CONTEXT = architecture/repo map/contracts/constraints/operations.
  STATE = current objective/active work/blockers/risks/next actions (VOLATILE).
  TDD = test strategy/commands/quality gates/missing coverage.
  DESIGN = visual language/tokens/states/accessibility (or explicit "no UI" with evidence).
  DECISIONS = dated decisions/alternatives/evidence/consequences/status (DURABLE) — seed
  from the verifier's decisions list.
- For each material claim add a compact source pointer (path/symbol/test/non-secret command).
  Prefer useful summaries over file inventories. No secret values. No TODO placeholders,
  no fake dates, no invented owners. Use today's date only where the runtime provides it;
  otherwise write "date: unverified".
- Each doc: a clean, ready-to-commit Markdown document (H1 title + sections). Self-contained.

Return the six doc bodies as strings.`,
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA, effort: 'high' })

return { repo: REPO, docs, verifier, discovery: named }
