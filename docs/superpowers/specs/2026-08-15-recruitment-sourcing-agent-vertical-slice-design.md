# Recruitment Sourcing Agent: Production Vertical Slice Design

**Date:** 2026-08-15

**Status:** Approved design

**Audience:** Product, engineering, security, and recruiting operations

## 1. Purpose

Build the first production vertical slice of a multi-tenant sourcing SaaS for recruitment agencies operating in India and the United States. A recruiter submits a job description, confirms an AI-generated scorecard, and starts an autonomous search through the platform's licensed provider API. The system sources 100–300 candidate profiles, resolves duplicates, ranks candidates with visible evidence, enriches the top 50 with permitted contact data, and presents the results in a focused CRM review workspace.

The product assists sourcing and review. It does not contact candidates or make hiring decisions.

## 2. Product Decisions

- The paying customer is a recruitment agency managing multiple client companies.
- The launch markets are India and the United States.
- This is a production SaaS vertical slice, not a prototype.
- The launch capacity target is 25 agencies, 250 active recruiter users, and 25 concurrent sourcing runs.
- Sourcing uses licensed provider APIs. Unauthorized scraping, browser automation, access-control bypass, and CAPTCHA evasion are excluded.
- Apollo is the only live sourcing and enrichment connector in this slice. The SaaS operator owns and manages the provider contract and API credentials; agency recruiters do not connect individual Apollo accounts. LinkedIn, Naukri, and Indeed remain disabled connector contracts until approved access and data rights are in place.
- Recruiters confirm an editable scorecard before sourcing begins.
- One job should source 100–300 candidates and surface the best 20–50.
- Candidates who miss a confirmed must-have appear as explained near matches, not in the main ranking.
- The primary success metric is a recruiter acceptance rate of at least 70% among the top 20 results. A candidate counts as accepted when marked Reviewed or Shortlisted rather than Rejected.
- Contact enrichment runs automatically for the top 50 ranked candidates and on demand for additional candidates.
- The CRM stages are New, Reviewed, Shortlisted, and Rejected. Near Match is a match classification and view, not a normal pipeline stage.
- Inactive contact information expires after 180 days.

## 3. Scope

### 3.1 Included

- Operator-provisioned agency tenant creation and user authentication
- Owner, admin, and recruiter roles
- Client-company records
- Job-description intake
- Company and industry classification
- Editable, versioned job scorecards
- Provider query planning
- Apollo people search and enrichment
- Asynchronous sourcing progress and partial results
- Candidate normalization and agency-scoped identity resolution
- Deterministic must-have evaluation and weighted ranking
- Evidence, uncertainty, gaps, and near-match explanations
- Top-50 automatic contact enrichment and on-demand enrichment
- Recruiter review workspace and all-candidates table
- Notes, tags, ownership, activity history, and rejection reasons
- CSV shortlist export
- Provider usage and per-job credit controls
- Auditing, retention, suppression, and privacy-request workflows
- Production monitoring, backups, and recovery procedures

### 3.2 Explicitly deferred

- Automated email, SMS, WhatsApp, or LinkedIn outreach
- Outreach drafting, sequences, and follow-ups
- Interview, offer, placement, invoicing, and billing pipelines
- Custom CRM stages and workflow automations
- SaaS subscription billing and plan management
- Self-service agency signup and checkout
- Live LinkedIn, Naukri, or Indeed integrations
- Cross-agency candidate sharing or a global talent graph
- Automated learning from recruiter decisions
- Native mobile applications
- A separate search engine or vector database

## 4. Architecture

Use a modular monolith with durable background workers.

### 4.1 Web application

A Next.js/TypeScript application provides the agency CRM. It authenticates against an OIDC-compatible identity provider and calls the Python API. It does not call data providers or language models directly.

### 4.2 Python application

A FastAPI application contains isolated modules with explicit service interfaces:

- **Identity and tenancy:** maps external identities to agency memberships and roles.
- **Clients:** owns client-company data and industry classifications.
- **Jobs and scorecards:** owns job intake, scorecard drafting, confirmation, and versions.
- **Provider gateway:** defines provider-neutral search and enrichment contracts.
- **Candidate identity:** normalizes profiles, source identities, and contact points.
- **Matching:** evaluates hard gates, weighted scores, evidence, and uncertainty.
- **CRM:** owns job-candidate stages, decisions, notes, tags, ownership, and exports.
- **Privacy and retention:** owns expiry, suppression, access, correction, and deletion workflows.
- **Usage and audit:** owns provider-credit accounting and append-only activity records.

Modules share one deployable application and PostgreSQL cluster but may not read or write another module's tables directly. Cross-module operations use service interfaces and stable identifiers. This keeps the initial deployment simple while preserving future service boundaries.

### 4.3 Background execution

Celery workers use Redis as the broker. PostgreSQL is the source of truth for workflow state, idempotency keys, checkpoints, counts, and cost usage. A lost or duplicated broker message must therefore be safe: a worker acquires the stage checkpoint, verifies the persisted state, and performs an idempotent transition.

### 4.4 Storage

- **PostgreSQL:** tenant-scoped application records, normalized candidate facts, JSON evidence, full-text search, trigram matching, workflow state, and audit data.
- **Redis:** queue transport, short-lived locks, and rate-limit coordination; never the source of truth.
- **S3-compatible object storage:** encrypted, short-lived provider snapshots needed for replay and support.

PostgreSQL full-text and trigram search are sufficient for the slice. A vector database is not introduced until measured search or ranking needs justify it.

### 4.5 External gateways

- **Apollo connector:** people search, person enrichment, phone-result webhooks, provider usage, and normalized errors using platform-operated credentials. A tenant usage ledger enforces agency and job budgets without exposing the credentials.
- **Language-model gateway:** structured job extraction, company-context classification assistance, and evidence-backed explanations. Outputs must conform to versioned schemas.
- **Future connectors:** LinkedIn, Naukri, and Indeed implement the same provider gateway only after approved access, contractual review, and field-level data mapping.

## 5. Tenant and Domain Model

The agency tenant is the security boundary. Every business record carries an immutable `tenant_id`. Client-company authorization may further restrict recruiters to assigned clients and jobs.

### 5.1 Hiring-demand records

- **Client Company:** agency-owned customer, domains, normalized industry, and approved adjacent industries.
- **Job:** job description, location, owner, status, client, and current sourcing-run state.
- **Scorecard Version:** immutable confirmed criteria including must-haves, preferences, exclusions, weights, locations, titles, seniority, experience, industry taxonomy version, and extraction provenance.
- **Sourcing Run:** query plan, scorecard version, provider, state, counters, credit budget, errors, and checkpoints.

### 5.2 Talent records

- **Canonical Candidate:** one normalized person per agency. It contains searchable profile facts but no job-specific decision.
- **Source Identity:** provider, provider person ID, profile URL, source timestamps, field provenance, and snapshot reference.
- **Contact Point:** email or phone, work/personal type, verification state, confidence, source provider, last verification, expiry, encryption metadata, and suppression state.

The same real person may have separate canonical records in different agencies. The system never links or exposes those records across tenants.

### 5.3 Job-candidate record

A job-candidate record joins one job to one canonical candidate and owns:

- hard-gate outcome and near-match reasons
- overall score and component scores
- evidence, uncertainty, and gaps
- scorecard and matching-model versions
- CRM stage
- owner, notes, tags, rejection reason, and timestamps

This separation lets one agency candidate match several jobs without duplicating identity or sharing job-specific decisions.

### 5.4 Identity resolution

Resolve identity in this order:

1. Exact provider plus provider-person ID
2. Verified email within the same agency
3. Normalized provider profile URL within the same agency
4. High-confidence name, employer, title, and location comparison

The first three may merge automatically when they do not conflict. Fuzzy matches never merge silently; they create a reviewable duplicate suggestion. Merge and split operations are audited and preserve source provenance.

## 6. Job Intake and Scorecard

The recruiter selects or creates a client, pastes a job description, and supplies missing context such as work location or employment model. The language-model gateway produces a draft scorecard with:

- target and alternate titles
- required skills and capabilities
- must-have experience
- preferred experience
- explicit exclusions
- seniority and years-of-experience ranges
- locations and work-eligibility requirements when provided
- hiring-company industry
- suggested adjacent industries
- uncertainties requiring recruiter confirmation

The UI distinguishes extracted statements from inferred suggestions. It must not invent a legal work-eligibility requirement, protected-class preference, or unstated exclusion. A recruiter edits and confirms the scorecard. Confirmation creates an immutable version; later edits create a new version and require a new sourcing run or explicit rescore.

## 7. Sourcing and Enrichment Flow

1. **Parse:** convert the job description and client context into a draft scorecard.
2. **Confirm:** require recruiter review and lock a scorecard version.
3. **Plan:** generate several narrow provider searches using titles, skills, seniority, location, exact industries, and recruiter-approved adjacent industries.
4. **Source:** retrieve at most 300 provider profiles while recording query and quota usage.
5. **Resolve:** normalize profiles and deduplicate them into agency candidate records.
6. **Match:** apply hard gates, score eligible candidates, and build the near-match list.
7. **Enrich:** automatically enrich the top 50; allow explicit on-demand enrichment for the rest.
8. **Review:** stream partial results and finish in the recruiter review workspace.

Search and enrichment are separate operations. A failed enrichment does not remove or invalidate an otherwise reviewable candidate.

## 8. Matching Model

Matching is deterministic, versioned, and evidence-based. The language model may structure facts and produce readable explanations, but it may not freely assign the final score.

### 8.1 Hard gates

Evaluate every confirmed must-have first. A candidate who demonstrably misses a must-have is classified as a near match. Missing provider information is `unknown`, not `failed`, unless the scorecard explicitly defines absence of evidence as disqualifying. The main UI shows the failed or unknown requirement.

### 8.2 Weighted score

Eligible candidates receive a 100-point score:

| Component | Weight |
| --- | ---: |
| Role and required-skill evidence | 35 |
| Relevant scope, seniority, and years of experience | 25 |
| Exact or approved-adjacent industry experience | 20 |
| Location and work-eligibility alignment | 10 |
| Experience recency and career trajectory | 10 |

Each component stores normalized evidence facts, source references, missing information, and its calculation. Industry matching uses a visible, versioned taxonomy. Exact-industry experience scores highest; only recruiter-approved adjacent industries receive partial credit.

Points are awarded only for supported evidence. An unknown criterion contributes zero points for that criterion and is shown as uncertainty; the score is not renormalized to hide missing data. Unknown must-haves remain eligible for the main ranking unless the confirmed scorecard explicitly makes evidence mandatory.

### 8.3 Feedback

Reviewed, Shortlisted, and Rejected decisions feed acceptance reporting. They do not automatically modify weights, prompts, or models in this slice. A later optimization project may propose calibrated changes using versioned offline evaluations and explicit approval.

## 9. Recruiter Experience

The application shell provides Agency, Clients, Jobs, Candidates, and Settings navigation. A client/job sidebar keeps active sourcing work visible.

The job lifecycle is:

1. Choose client
2. Paste job description
3. Edit and confirm scorecard
4. Start sourcing
5. Monitor progress and review partial or complete results
6. Export the shortlist

The job workspace contains:

- **Review:** ranked list on the left and detailed candidate evidence on the right.
- **All Candidates:** sortable, filterable table with bulk stage and ownership actions.
- **Near Matches:** candidates outside the main ranking with explicit failed or uncertain must-haves.
- **Scorecard:** confirmed criteria and versions.
- **Run Activity:** state, counts, provider usage, retries, and non-sensitive failures.

The candidate detail shows normalized experience, match score, component evidence, gaps, contact availability, provider provenance, notes, and activity. Primary actions are Mark Reviewed, Shortlist, Reject with reason, Add Note, Tag, Assign Owner, and Reveal Contact when allowed.

## 10. Workflow and Failure Handling

The sourcing state machine is:

`Draft → Awaiting Scorecard → Queued → Sourcing → Matching → Enriching → Ready`

Terminal or exceptional states are `Partially Ready`, `Cancelled`, and `Failed`. Every transition records a timestamp, actor, run ID, counters, and checkpoint.

- Rate limits pause the affected stage and resume after the provider window resets.
- Temporary timeouts and provider 5xx errors retry with bounded exponential backoff and jitter.
- Authentication, permission, or contract errors disable the platform connector and alert platform operators. Tenant quota exhaustion pauses only the affected tenant and notifies its agency owner or admin.
- Provider payloads that fail schema validation are quarantined and do not corrupt canonical records.
- Phone-enrichment webhooks are authenticated, idempotent, deduplicated, and replay-safe.
- Invalid language-model output is schema-rejected and retried once. Persistent failure produces an editable manual scorecard or a visible missing explanation; it never invents candidate facts.
- Cancellation stops new work but preserves valid records already committed.
- Per-agency and per-job search, enrichment, and credit limits prevent runaway cost.
- A partially ready run remains reviewable and clearly identifies incomplete stages.

## 11. Security and Privacy

- Use an external OIDC-compatible identity provider with short-lived sessions and multi-factor authentication support.
- Tenant roles are Owner, Admin, and Recruiter. Owners and admins manage membership and agency settings; recruiters access only authorized clients and jobs. Platform operators use a separate operational identity and are not members of customer tenants.
- Enforce tenant predicates in application services and PostgreSQL row-level security.
- Store platform-owned provider credentials in a managed secrets service accessible only to the provider gateway and authorized platform operators; rotate them without deployment.
- Encrypt all traffic and storage. Additionally encrypt email addresses, phone numbers, and sensitive snapshot references at the application field level.
- Redact contact data, provider credentials, and raw payloads from logs, traces, analytics, and error messages.
- Keep append-only audit events for authentication-sensitive actions, provider use, contact reveal, exports, role changes, merges, and privacy operations.
- Permit all provider-returned work and personal contact fields only when the provider contract and applicable regional rules allow them.
- Expire encrypted provider snapshots after 30 days.
- Expire or re-verify contact points 180 days after their last verification or recorded legitimate recruiting use.
- Implement candidate access, correction, deletion, and opt-out workflows. Completed deletion retains only a tenant-keyed HMAC of normalized identifiers needed to suppress later re-import; it retains no reversible contact value.
- Provider-specific deletion and use restrictions take precedence over platform defaults.

This design establishes engineering controls, not legal advice. Production launch requires counsel or a qualified privacy reviewer to approve the final notices, lawful-basis process, regional retention configuration, provider contracts, and candidate-rights procedures for India and the United States.

## 12. Observability and Operations

Use a shared run ID across API requests, worker tasks, audit events, logs, metrics, and traces. Structured telemetry excludes candidate contact data and raw provider payloads.

Alert platform operators on:

- sourcing runs stuck beyond their stage threshold
- sustained provider errors or authentication failures
- quota or credit-budget exhaustion
- webhook authentication failures
- queue backlog and worker unavailability
- cross-tenant authorization denials above baseline
- retention, deletion, or snapshot-expiry failures
- backup or restore verification failures

Common foreground API reads should remain responsive while sourcing runs execute. Sourcing duration is reported as a workflow metric rather than a synchronous request objective because provider latency and rate limits are external dependencies.

## 13. Testing Strategy

### 13.1 Unit and property tests

Cover scorecard validation, hard gates, scoring, industry adjacency, query planning, normalization, identity resolution, expiry, and suppression. Reordering, retrying, or replaying identical inputs must not alter results or create duplicate records.

### 13.2 Provider contract tests

Test search, enrichment, rate limits, authentication errors, malformed responses, and webhook events using redacted recorded fixtures and a provider test environment when available. Provider-specific payloads must remain behind connector contracts.

### 13.3 Integration and isolation tests

Exercise PostgreSQL row policies, service authorization, workers, checkpoints, retries, cancellation, partial completion, snapshot expiry, contact expiry, and privacy workflows. Adversarial tests attempt cross-agency reads and writes through every API, export, and worker path.

### 13.4 End-to-end tests

Verify:

- agency setup through confirmed scorecard
- sourcing through partial and complete ranked results
- top-50 automatic enrichment and on-demand enrichment
- review, shortlist, reject, note, filter, assign, tag, and export
- rate limiting, provider outage, cancellation, and successful resume
- deletion or opt-out followed by suppression during a later run

### 13.5 Matching evaluation

Maintain a versioned evaluation set covering at least 30 representative India and US jobs. Recruiters label must-have outcomes and candidate relevance. A release cannot reduce hard-gate accuracy or materially degrade ranking quality against the current baseline. In production, report the top-20 acceptance rate by job, client, market, and scoring version. Measure it seven days after results become Ready, or earlier when all top-20 candidates have left New. The numerator is candidates currently Reviewed or Shortlisted; the denominator is 20, so New and Rejected candidates are not accepted. The target is at least 70%.

### 13.6 Production readiness

Load tests simulate 25 agencies, 250 active users, and 25 concurrent sourcing runs of 300 profiles each. Release gates require:

- no lost or duplicated canonical or job-candidate records
- successful queue recovery and idempotent replay
- responsive foreground APIs under target load
- verified tenant isolation
- dependency, secret, and application security scans
- successful backup and restore rehearsal
- verified privacy and retention workflows
- production dashboards and alerts
- documented rollback procedure

## 14. Success Criteria

The vertical slice is complete when:

1. A recruiter can create a client and job, confirm a scorecard, and start a real Apollo-backed sourcing run.
2. A run can collect and deduplicate 100–300 candidates, rank them, enrich the top 50, and expose partial progress without blocking the UI.
3. The review workspace explains every main-ranking and near-match decision using versioned evidence and gaps.
4. Agency users can review, shortlist, reject, annotate, assign, filter, and export candidates without cross-tenant leakage.
5. Provider failures, retries, cancellation, quota limits, and webhook replays are safe and observable.
6. Contact expiry, deletion, opt-out, and suppression workflows pass verification.
7. The system passes the launch-capacity and release gates in Section 13.
8. Pilot reporting can measure the target top-20 recruiter acceptance rate of at least 70%.

## 15. Future Subprojects

After this slice is implemented and validated:

1. Add SaaS plans, billing, quotas, agency administration, and support tooling.
2. Add approved LinkedIn, Naukri, Indeed, and alternative data-provider connectors.
3. Add ranking calibration, analytics, enrichment waterfalls, and unit-economics optimization.
4. Consider outreach or downstream ATS integrations as separate consent, compliance, and workflow designs.

## 16. Reference Documentation

- [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement)
- [LinkedIn Talent Solutions overview](https://learn.microsoft.com/en-us/linkedin/talent/)
- [Apollo People API Search](https://docs.apollo.io/reference/people-api-search)
- [Apollo People Enrichment](https://docs.apollo.io/reference/people-enrichment)
