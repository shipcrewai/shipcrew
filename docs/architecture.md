# Shipply Architecture

## Pipeline Overview

```
              ┌─────────────────────────────────────┐
              │          [ 🧠 Scout (Bot) ]         │
              │   Intake & Multi-User Interview    │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │      [ 🔍 Doc Review (Bot) ]        │
              │   ce-doc-review Requirements Audit  │
              └──────────────────┬──────────────────┘
                                 │
                        ┌────────┴────────┐
                        │  gaps found?    │
                        └────────┬────────┘
                                 │ yes → loop back to Scout
                                 │ no
                                 ▼
              ┌─────────────────────────────────────┐
              │       [ 📋 GATE 1 GOVERNANCE ]      │
              │       Product & Community RFC       │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │       [ 📐 Blueprint (Bot) ]        │
              │     Technical Architecture Plan     │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │       [ 🛠️ GATE 2 GOVERNANCE ]      │
              │       Developer & Maintainer RFC    │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │        [ 🐝 Forge / Swarm ]         │
              │   Parallel Bead Execution Engine    │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │       [ 🚀 GATE 3 GOVERNANCE ]      │
              │      GitHub Pull Request Review     │
              └─────────────────────────────────────┘
```

## Named Bot Personas & Lifecycle Stages

Rather than deploying a single monolithic "AI" identity, the system introduces a specialized team of digital collaborators. Each has a dedicated task profile, isolated messaging boundaries, and fine-tuned system configurations.

### 🧠 Persona A: Scout — The Collaborative Intake Agent

- **Role:** Product Manager / User Researcher.
- **Responsibilities:** Listens for raw project sparks or simple feature queries across open communication channels.
- **Dynamic Interview Pattern:** Rather than forcing the user to complete static forms or complex templates, Scout initiates a fluid, context-aware conversational thread. It asks clarifying questions one by one, intentionally drawing out assumptions and exposing functional gaps.
- **Multi-User Collaboration:** Because this dialogue occurs within an open thread inside the messaging platform, other team members can jump into the conversation at any time. If a non-technical author leaves a question open, a developer can reply in-thread to provide precise parameters. Scout processes all collaborative inputs to continuously refine the state object.
- **Output:** Translates the group conversation into a formalized, clear Brainstorm / Requirements Document. This document MUST include a **Goals Statement** — a concise, non-negotiable section that captures the problem being solved, the success criteria, and explicit anti-goals (what this proposal deliberately does NOT address). The Goals Statement serves as the decision anchor for all downstream gates: any RFC comment, architectural choice, or implementation tradeoff must be evaluated against these stated goals. A comment like "just use the existing schema" is only valid if it satisfies the goals; if it contradicts them, it is out of scope by definition.


**Example — Scout Interview State (`docs/gemini-code-1783978084422.json`):**

```json
{
  "proposal_id": "prop_98765",
  "status": "INTERVIEW_IN_PROGRESS",
  "author_id": "usr_alpha",
  "contributors": ["usr_alpha", "usr_beta"],
  "raw_spark": "Add user-defined tagging to project issues.",
  "interview_history": [
    {
      "speaker": "bot",
      "text": "How should these tags be scoped? Globally across the organization or strictly per-project?"
    },
    {
      "speaker": "usr_alpha",
      "text": "Per-project by default, but admins should be able to promote them."
    },
    {
      "speaker": "usr_beta",
      "text": "Agreed, we also need to make sure tags can have custom hex colors."
    }
  ],
  "missing_gaps_identified": [
    "Database schema changes for tag colors",
    "Permission levels required to promote tags"
  ]
}
```

### 🔍 Persona D: Doc Review — The Requirements Auditor

- **Role:** Automated Quality Gate / Requirements Validator.
- **Responsibilities:** Before Scout's output reaches human RFC voting, Doc Review dispatches a swarm of parallel AI reviewer agents (coherence, feasibility, product-lens, adversarial, scope-guardian, security-lens, design-lens) against the Brainstorm / Requirements Document using the `ce-doc-review` framework. A primary audit target is the **Goals Statement** — Doc Review verifies it is present, internally consistent, testable, and free of contradictory anti-goals. A missing or incoherent Goals Statement is an automatic bounce.
- **Gap Detection:** Each persona surfaces role-specific issues — contradictions, missing actors, unstated assumptions, scope misalignment, security blind spots, and thin requirements that would collapse under implementation.
- **Bounce-Back Loop:** If Doc Review finds blocking gaps (missing acceptance criteria, unresolved open questions, contradictory requirements), the proposal is **bounced back to Scout** with a structured gap report. Scout re-engages the interview thread, surfaces the specific gaps to the stakeholders, and forces resolution before the document can proceed to Gate 1.
- **Output:** A tiered findings report (`safe_auto`, `gated_auto`, `manual`, `FYI`) with suggested fixes. `safe_auto` fixes are applied silently; remaining findings are surfaced in the interview thread for human resolution.
- **Exit Condition:** The document passes when all blocking gaps are resolved — either by Scout pulling answers from stakeholders, or by stakeholders explicitly deferring non-blocking items to the document's Open Questions section.
### 📐 Persona B: Blueprint — The Solution Architect

- **Role:** System Architect / Technical Lead.
- **Responsibilities:** Transforms an approved Gate 1 Requirements Document into a highly prescriptive code execution strategy.
- **Output:** Generates a structured JSON blueprint detailing modified directories, brand new system files, external library dependencies, database migration schemas, and an isolated breakdown of execution tasks ("beads").

### 🐝 Persona C: Forge — The Parallel Execution Swarm

- **Role:** Automated Software Engineer Swarm.
- **Responsibilities:** Consumes the validated engineering blueprint, breaks it down into individual decoupled task beads, and spins up task runners to implement the implementation plan in parallel branches.
- **Fault-Tolerant Engine:** If an execution runner creates code that triggers a failing lint check or a broken CI/CD pipeline test, Forge uses historical code context to attempt an automated self-healing cycle (maximum of 2 retries).

## The Three Governance Gates (RFC Framework)

To preserve architectural control and guard software quality without introducing development bottlenecks, the pipeline enforces three logical checkpoints.

### 📋 GATE 1: Product Governance & Roadmapping

- **The Interface:** Scout posts the final compiled Brainstorm / Requirements document into a collaborative discussion room.
- **The Process:** This stage operates as a Product RFC (Request for Comments). Team members, users, and external software services add direct platform comments to tweak feature scopes, voice edge cases, or refine priorities.
- **Automated Scheduling:** When an idea passes the human voting threshold, the bot query engine communicates with an integrated ticketing tool (e.g., GitHub Projects v2, Linear, or GitLab). It scans current milestone timelines, analyzes historical team velocity, assigns a realistic deployment target date, generates the issue epic, and updates the public product roadmap automatically.


**Example — Gate 1 RFC State (`docs/gemini-code-1783978087470.json`):**

```json
{
  "proposal_id": "prop_98765",
  "current_gate": "GATE_1_PRODUCT_RFC",
  "requirements_doc_url": "https://github.com/covenant-gov/proposals/blob/main/prop_98765.md",
  "voting_metrics": {
    "votes_for": 7,
    "votes_against": 1,
    "required_quorum": 5,
    "status": "PENDING"
  },
  "rfc_comments": [
    {
      "comment_id": "c_001",
      "user_id": "usr_gamma",
      "timestamp": "2026-07-13T20:45:00Z",
      "body": "Should we limit the maximum number of tags per project to prevent clutter?",
      "resolved": false
    }
  ],
  "scheduling_target": {
    "auto_calculated_milestone": "v2.4-Sprint3",
    "source_system": "github_projects_v2"
  }
}
```
### 🛠️ GATE 2: Technical Governance & Architecture Review

- **The Interface:** Blueprint publishes its detailed technical blueprint within an isolated developer/maintainer-only channel.
- **The Process:** Operates as a deep Technical RFC. Senior engineering maintainers evaluate the implementation model *before* any compute resources are spent on code generation. Maintainers submit structural modifications, adjust dependencies, or mandate alternative design paths directly in the comment thread. Blueprint listens to these adjustments and dynamically rebuilds the JSON implementation parameters.
- **Execution Trigger:** A maintainer issues an authorization payload via the bot API, unleashing the parallel execution engine.

### 🚀 GATE 3: Code Governance & Automated Alerting

- **Self-Healing Failure Escape:** If Forge exhausts its 2 automated self-healing retries on a code task without passing tests, it stops work on that specific bead. It formats a structural notification using `pacto-bot-api` and pushes it directly into the Gate 2 technical room thread. This diagnostic card contains the failure log, the breaking code diff, and a clear request for manual human engineering steering.
- **The Code Gate:** All successfully executed code for a proposal is grouped into a single Pull Request on GitHub for standard human peer review before final integration.


**Example — Forge Failure Alert (`docs/gemini-code-1783978089843.json`):**

```json
{
  "proposal_id": "prop_98765",
  "task_id": "bead_auth_04",
  "status": "EXECUTION_FAILED_ALERT",
  "error_summary": "Linter error: 'tag_color' is assigned a value but never used in project_model.py line 42.",
  "attempts_made": 2,
  "git_branch": "ai/feature-project-tags-bead-04",
  "logs_excerpt": "[ERROR] Flake8: F841 local variable 'tag_color' is assigned to but never used",
  "action_required": "Please review the branch or reply to this thread to instruct the agent on how to resolve the syntax issue."
}
```

## Gate-Transition Immutability Contract

To prevent concurrent mutation of in-flight proposals, the system enforces a **version-lock at every gate boundary**:

- **Gate 0 → Gate 1:** When Doc Review passes a requirements document, the Goals Statement and requirements are **frozen** as `rev1`. The document proceeds to Gate 1 (Product RFC). Any late-breaking comments on the original Scout thread that would materially change scope are routed to a **follow-up proposal** — they do not mutate the in-flight `rev1`. The original thread remains open for discussion, but the frozen revision is the authoritative input to Gate 1 voting.
- **Gate 1 → Gate 2:** When Gate 1 approves and Blueprint emits its technical plan, the requirements document is frozen as `rev2` (incorporating RFC amendments). Blueprint's output is a function of `rev2` only. If a Gate 1 comment arrives after Blueprint has begun, it is queued for the next revision cycle — Blueprint does not rebuild mid-flight.
- **Gate 2 → Gate 3:** When a maintainer authorizes execution, the Blueprint JSON is frozen. Forge's bead decomposition is a function of that frozen blueprint. A maintainer comment that arrives mid-execution is routed to the Gate 2 thread as a **steering note** — it does not invalidate in-flight beads. The maintainer must explicitly cancel and re-authorize to trigger a rebuild.

This contract eliminates the "late scope change poisons in-flight work" failure mode. Every stage operates on an immutable input revision. Changes are batched into the next cycle.

## Dead Code Removal Policy

Forge treats dead code (unused variables, unreachable branches, orphaned imports) as a **correctness defect**, not a style warning. The self-healing policy is:

- **Default action: remove.** If a lint rule like `F841` (unused variable) fires, Forge's first self-healing attempt removes the dead code. Dead code that compiles but serves no purpose is waste — it confuses maintainers, bloats diffs, and masks real bugs.
- **Exception: future-bead dependency.** If the dead code exists because a *different bead* (not yet merged) will consume it, the bead dependency graph tracks this. The consuming bead declares the dependency in its Blueprint spec, and the producing bead's lint suppression is justified by a cross-reference to the dependent bead ID. Forge does not remove code that has a tracked downstream consumer.
- **Escape hatch:** If Forge removes code and a human reviewer determines it was needed, the Gate 3 PR review catches it. The reviewer comments on the PR, and the bead is re-queued with the removal reverted and a suppression annotation added. This is rare — the dependency graph should capture cross-bead references before execution begins.
## Model Agnostic Routing Architecture

To remain independent of proprietary platform ecosystems, the core engineering pipelines are built entirely atop abstract translation wrappers like the Vercel AI SDK or standard OpenAI-Compatible routing networks.

This layer allows the platform operator to map distinct backend models or cost structures directly to specific parts of the project life cycle via a centralized configuration blueprint:

```json
{
  "system_id": "pacto-augmented-plm",
  "bots": {
    "Scout": {
      "provider": "openai-compatible",
      "baseURL": "http://localhost:11434/v1",
      "model": "llama3.1-instruct",
      "temperature": 0.7,
      "max_interview_turns": 8
    },
    "Blueprint": {
      "provider": "openai",
      "model": "gpt-4o",
      "temperature": 0.2
    },
    "Forge": {
      "provider": "anthropic-compatible-or-local",
      "baseURL": "https://api.your-internal-llm.network/v1",
      "model": "deepseek-coder",
      "temperature": 0.0
    }
  },
  "governance": {
    "gate_1_min_votes": 5,
    "gate_2_allowed_roles": ["maintainer", "admin"]
  }
}
```

## Event Queue & Messaging Backend

The system's event queue is the **Nostr protocol** via the `pacto-bot-api` JSON-RPC daemon. Every bot interaction — Scout's interview thread, Gate 1 RFC discussion, Gate 2 technical review, Forge failure alerts — flows through Nostr events delivered over a WebSocket transport.

### JSON-RPC Methods Used

| Method | Purpose |
|---|---|
| `handler.register` | Each bot persona registers as a handler for its `bot_id` and subscribes to `dm_received` and `mls_group_message_received` events |
| `agent.event` | Inbound notification — the daemon pushes decrypted messages to the registered handler |
| `handler.response` | Handler replies with `ack`, `reply`, `defer`, or `ignore` |
| `agent.send_dm` | Bot sends a direct message to a user |
| `agent.send_group_message` | Bot posts into an MLS-encrypted Squad (group) channel |
| `agent.set_profile` | Bot updates its Nostr kind:0 profile (display name, bio, avatar) |
| `agent.error` | Handler reports a processing error back to the daemon |
| `agent.is_squad_member` | Verify a user's membership in a Squad before processing their vote or comment |

### Channel Topology

| Channel | Protocol | Purpose |
|---|---|---|
| Scout DM thread | Nostr DM (gift-wrapped) | 1:1 and multi-party interview with the proposal author and contributors |
| Gate 1 Squad | MLS group (`mls_group_message_received`) | Product RFC — community discussion and voting |
| Gate 2 Squad | MLS group | Technical RFC — maintainer-only architectural review |
| Forge alert thread | Nostr DM to Gate 2 Squad | Failure escape — diagnostic cards posted when self-healing is exhausted |

### State Machine Driver

The state machine is **event-driven**: each `agent.event` notification advances the proposal through its lifecycle. There is no polling loop. The handler's `handler.response` action determines the next state transition:

- `ack` — message processed, no reply needed (e.g., a vote counted)
- `reply` — bot responds and waits for the next message (e.g., Scout asks a follow-up question)
- `defer` — message acknowledged but processing continues asynchronously (e.g., Blueprint begins plan generation)
- `ignore` — message is out of scope for this bot (e.g., casual chatter in a Squad)

## Task Backend Interface

Beads created by Blueprint and executed by Forge are stored in a **swappable task backend** behind a `TaskBackend` interface. The default implementation is [Beads](https://beads.gascity.com) (`bd`), a Dolt-backed, dependency-aware issue tracker built for AI coding agents. Alternative backends (Linear, GitHub Issues, GitLab) implement the same interface.

### Interface Contract

```python
class TaskBackend(Protocol):
    """Abstract task store for proposal beads."""

    def create_bead(self, spec: BeadSpec) -> Bead:
        """Create a single bead from a Blueprint bead spec."""
        ...

    def create_molecule(self, spec: MoleculeSpec) -> Molecule:
        """Instantiate a molecule (epic with dependency-ordered children) from a Blueprint plan."""
        ...

    def get_ready(self, molecule_id: str) -> list[Bead]:
        """Return the claimable frontier — beads with no open blockers."""
        ...

    def claim(self, bead_id: str, agent_id: str) -> Bead:
        """Atomically claim a bead for execution."""
        ...

    def close(self, bead_id: str, reason: str) -> Bead:
        """Mark a bead as closed, unblocking its dependents."""
        ...

    def get_blocked(self, molecule_id: str) -> list[Bead]:
        """Return beads waiting on open blockers."""
        ...

    def add_dependency(self, dependent_id: str, blocker_id: str, dep_type: str = "blocks") -> None:
        """Add a dependency edge between two beads."""
        ...

    def list_by_proposal(self, proposal_id: str) -> list[Bead]:
        """Return all beads associated with a proposal."""
        ...

    def sync(self) -> None:
        """Push/pull to the shared remote (Dolt push/pull or equivalent)."""
        ...
```

### Storage Modes (Beads Backend)

| Mode | Command | Data lives at | Writers |
|---|---|---|---|
| **Embedded** (default) | `bd init` | `.beads/embeddeddolt/` | one (file-locked) |
| **Server** | `bd init --server` | `.beads/dolt/` | many concurrent |
| **Remote** | DoltHub / DoltLab | remote Dolt repository | many, via push/pull |

For a multi-agent swarm like Forge, **server mode** or a **DoltHub remote** is required — embedded mode's file lock prevents concurrent bead claims.

### Bead Lifecycle in the Pipeline

```
Blueprint emits BeadSpec[]  →  bd mol pour  →  Molecule (epic + children)
                                                │
                              bd ready          ←  Forge queries claimable frontier
                              bd update --claim ←  Forge claims a bead
                              bd close          ←  Forge closes on success
                              bd gate create    ←  Forge creates a gate on failure
                              bd dolt push      ←  Sync to shared remote
```

### Dependency Types Used

| Type | Semantics | Pipeline Usage |
|---|---|---|
| `blocks` | B can't start until A closes | Bead execution ordering |
| `parent-child` | Epic/subtask hierarchy | Molecule structure |
| `conditional-blocks` | B runs only if A fails | Self-healing retry paths |
| `waits-for` | B waits for all of A's dynamic children | Fan-in after parallel bead execution |
| `discovered-from` | Provenance tracking | Bugs found during bead execution |

## Scout Interview Termination Heuristic

Scout does not rely solely on `max_interview_turns` to decide when to produce a requirements document. Termination is **gap-driven with a confidence floor**:

1. **Gap-driven:** Scout maintains a `missing_gaps_identified` list throughout the interview. Each gap is a concrete unanswered question (e.g., "Permission levels required to promote tags"). The interview continues until all gaps are resolved — either answered by a stakeholder or explicitly deferred to the document's Open Questions section.

2. **Confidence floor:** Even with zero open gaps, Scout evaluates its confidence in the requirements document. If the model signals low confidence (ambiguous answers, contradictory stakeholder input, missing domain context), Scout asks one final synthesis question: "Here's what I understand. Does this match your intent?" A stakeholder confirmation closes the interview.

3. **Hard ceiling:** `max_interview_turns` (default 8) is a safety valve. If reached with unresolved gaps, Scout produces the best-effort document and flags the remaining gaps as `UNRESOLVED` — Doc Review will almost certainly bounce it, but the loop is bounded.

4. **Explicit termination:** A stakeholder can issue `/done` at any time to force document generation. Scout includes a warning in the output that the interview was terminated early.

## Cross-Proposal Conflict Detection

When two proposals touch overlapping code, the system detects the conflict and prevents both from proceeding blindly into execution.

### Detection Points

| Stage | What's Checked | Action |
|---|---|---|
| **Gate 1** | Existing proposals and their affected file paths (from prior Blueprint outputs) | Surface dependencies: if proposal B depends on work in proposal A, Gate 1 flags A as a prerequisite and notifies B's sponsor |
| **Blueprint generation** | All in-flight Blueprint plans and their file deltas | If Blueprint detects a file overlap with another in-flight plan, it **pauses** and notifies both proposal sponsors via the Gate 2 Squad |
| **Forge bead claim** | Bead file targets vs. other in-flight beads | If a bead would touch a file currently being modified by another active bead, Forge skips it and reports the conflict |

### Conflict Resolution Protocol

When a conflict is detected at Blueprint time:

1. Both proposal sponsors receive a notification in their respective Gate 2 threads: "Proposal `prop_A` and `prop_B` both touch `src/auth/middleware.ts`. Neither will proceed until one is cancelled or one passes Gate 2."
2. The proposals enter a **mutual block** state — neither Blueprint finalizes until the conflict is resolved.
3. Resolution options:
   - One sponsor cancels their proposal
   - One proposal passes Gate 2 first, and the other's Blueprint rebases on the merged output
   - Sponsors coordinate to merge the proposals into a single combined plan

### Gate 1 Dependency Surfacing

During Gate 1 RFC, the system scans the active proposal registry and the Beads task store for existing work that the current proposal depends on:

- **Active proposals** that define prerequisites (e.g., "this feature requires the new auth middleware from prop_12345")
- **Open beads** in the task store that are tagged as dependencies of the proposal's target area
- **Closed beads** that introduced APIs or schemas the proposal will extend

These are surfaced in the Gate 1 RFC thread as a **Dependency Card** — a structured message listing each dependency with its status (merged, in-flight, blocked) and a link to the relevant proposal or bead. This ensures voters and sponsors understand the full cost and risk before approving.
