---
date: 2026-07-14
topic: shared-omp-config-and-github-app
---

## Summary

Provide a container-native credential and integration model for Shipply bots: centralized, read-only provisioning of the OMP environment via a shared Docker volume, and an org-level GitHub App with fork-based PRs owned by a dedicated workspace organization. This is a secrets-delivery and integration design; fine-grained authorization over which handler sees which credential is out of scope for this work.

---

## Problem Frame

Today the Shipply handler containers spawn `omp acp` but have no path to provider credentials inside the container: the root `docker-compose.yml` does not mount `~/.omp`, and the harness never passes model credentials to the subprocess. At the same time, GitHub access is planned around a PAT with `repo:read` scope, which does not scale to many repos, cannot receive events, and cannot open PRs from forks. The deployment needs a credential model that works for multi-repo orgs, supports event-driven PR monitoring, and keeps secrets out of images and host mounts.

---

## Key Decisions

- **Centralized OMP provisioning.** Instead of mounting the host `~/.omp` directory, the deployment sets `PI_CONFIG_DIR` to a dedicated Docker volume that is provisioned at startup by an init service. The volume contains `config.yml`, `models.yml`, plugins, and skills, and is mounted **read-only** into handler containers. OMP's runtime state is redirected to a separate writable per-handler location by setting `PI_CODING_AGENT_DIR` to a different path.
- **Supported providers and model roles.** The initial provider set is Ollama / Ollama Cloud, OpenRouter, Kimi Coding, and Claude. The model aliases in `shipply.toml` (`fast`, `default`, `slow`) map to OMP model roles as `fast` → `smol`, `default` → `default`, `slow` → `slow`. Actual provider/model IDs are declared in `models.yml` or `config.yml` under `modelRoles`.
- **GitHub App at the org level.** The same GitHub App is installed on two accounts: the source organization (to read code, create PRs, and receive events) and the workspace organization (to create fork repositories and push execution branches).
- **Forks owned by a workspace org.** PRs are opened from a dedicated workspace organization's fork namespace, not from branches inside the source org. This keeps source-org write permissions minimal and avoids managing a machine user account.
- **Dedicated Nostr event bridge.** GitHub webhook events are received by a small public-facing bridge that converts validated GitHub payloads into Nostr events and publishes them through the existing Pacto/Nostr infrastructure. Gate 3 already consumes Nostr events, so no new internal protocol is needed. The bridge can be exposed locally via ngrok for development or via a published app (e.g., Vercel) for production. The default implementation uses [getAlby/http-nostr](https://github.com/getAlby/http-nostr).
- **OMP credentials via auth broker.** Provider credentials are resolved through an OMP auth broker (`omp auth-broker serve`) rather than being written into the shared volume. Handlers set `OMP_AUTH_BROKER_URL` and `OMP_AUTH_BROKER_TOKEN` so tokens are fetched from the broker at runtime and refreshed automatically. This keeps provider keys out of the shared volume and reduces rotation to a broker-side change.

---

## Requirements

### OMP configuration

- R1. Handler containers must run `omp acp` without mounting the host `~/.omp` directory.
- R2. An initialization/refresh service must provision the full OMP environment from Docker secrets and write it to a shared Docker volume referenced by `PI_CONFIG_DIR`. Provisioning includes `config.yml`, `models.yml`, required plugins, and required skills. The service runs at deployment start and whenever a deployment secret is rotated; it is idempotent or replaces the previous volume contents atomically.
- R3. The provisioned OMP environment referenced by `PI_CONFIG_DIR` must be mounted **read-only** into every handler container that uses the harness. OMP runtime state must be redirected to a separate writable per-handler location by setting `PI_CODING_AGENT_DIR` to a different path.
- R4. The configuration must support the initial provider set: Ollama / Ollama Cloud, OpenRouter, Kimi Coding, and Claude. The model aliases in `shipply.toml` map to OMP model roles as `fast` → `smol`, `default` → `default`, `slow` → `slow`. Actual provider/model IDs are declared in `models.yml` or `config.yml` under `modelRoles`. Required plugins and skills must be installed and version-pinned from a tracked manifest (e.g., `omp-plugins.lock` or `omp-requirements.txt`).
- R5. Rotating a provider key, model alias, plugin version, or skill version must require only a Docker secret update and a container recreation, not an image rebuild.
- R6. Before the init service is considered complete, it must verify that the OMP configuration is valid and that all required plugins and skills loaded successfully.

### GitHub access and PR workflow

- R7. The deployment must authenticate to GitHub using a single GitHub App installed on **two accounts**: the source organization and the workspace organization. A personal access token is not acceptable.
- R8. The source-org installation must have repository permissions `contents:read`, `pull_requests:write`, and `metadata:read`, plus subscriptions to `pull_request` and `pull_request_review` events.
- R9. The workspace-org installation must have repository permissions `administration:write` (to create repositories) and `contents:write` (to push branches), and must be granted access to all repositories in the workspace org.
- R10. Code changes must be made on a fork of the source repo owned by the dedicated workspace organization, then proposed back to the source repo via a pull request.
- R11. The deployment must manage two short-lived installation tokens: one for the source org (used to create and update PR metadata on the source repo) and one for the workspace org (used to create the fork and push the execution branch). Tokens must be generated from the GitHub App private key, cached, and refreshed before expiry.
- R12. PR creation and update must be idempotent: branch names must be deterministic per bead/task, and the system must check for an existing open PR before creating a new one.
- R13. A single Shipply deployment must be configurable to serve a subset of repos within one organization, and must drop events and API calls for out-of-scope repos.

### Secrets and operational model

- R14. GitHub long-lived credentials (GitHub App private key, GitHub App webhook secret) must be injected as Docker secrets. LLM provider credentials are resolved at runtime through the OMP auth broker; they are not injected as Docker secrets and are not written into the shared OMP volume. Environment variables may be used only for non-sensitive configuration values such as endpoints, org names, and log levels.
- R15. Credentials must not be committed to the repository or included in the container image layers. The build context must exclude `secrets/`, `.env*`, `*.pem`, and any local credential files.
- R16. The design must allow credential rotation without rebuilding the Shipply handler image. Rotating the GitHub App private key or webhook secret requires updating the corresponding Docker secret and restarting the affected containers. Rotating LLM provider credentials requires updating the auth broker.
- R17. The operator must be able to scope the GitHub App source-org installation to a subset of repos within the org.

### Webhook handling

- R18. GitHub webhook payloads must be validated using HMAC-SHA256 signature verification with the webhook secret, constant-time comparison, rejection of missing/invalid signatures, an allow-list of expected event types, and idempotency based on `X-GitHub-Delivery`.
- R19. The webhook endpoint must be served over HTTPS with a valid, trusted certificate. For development this can be a public ingress tunnel (e.g., ngrok); for production it should be a published app or reverse proxy.
- R20. Validated webhook events must be converted to Nostr events by a bridge service and published through the existing Pacto/Nostr infrastructure so that Gate 3 receives them as `dm_received` or `mls_group_message_received` events. The default bridge uses [getAlby/http-nostr](https://github.com/getAlby/http-nostr).
- R21. The webhook/Nostr bridge must be isolated from the handlers, but it may share the Pacto daemon and socket used by the rest of the deployment.
- R22. The deployment must include a fallback reconciliation mechanism that catches missed webhook events, because GitHub retries are time-bound and events can be lost during outages.

---

## Actors

- A1. **Shipply operator** — provisions secrets, installs the GitHub App on both orgs, creates the workspace org, and configures the deployment.
- A2. **OMP environment init container** — runs at startup and on secret rotation to provision the shared OMP environment from secrets, including provider config, plugins, and skills.
- A3. **Shipply handler** — a bot persona (Scout, Blueprint, Forge, Gate 3, etc.) that runs `omp` and interacts with GitHub.
- A4. **GitHub App token manager** — generates, caches, and refreshes short-lived installation tokens for the source org and the workspace org.
- A5. **Workspace organization** — owns the fork namespace where execution branches live.
- A6. **Org maintainer** — reviews and merges PRs created by the bot.
- A7. **GitHub → Nostr event bridge** — receives and validates GitHub webhooks, converts them to Nostr events, and publishes them through the Pacto/Nostr infrastructure. Implemented with getAlby/http-nostr by default.

---

## Key Flows

### F1. OMP configuration initialization

- **Trigger:** Deployment starts or a deployment secret is rotated.
- **Actors:** A2, A3.
- **Steps:**
  1. The init service reads provider secrets, model aliases, plugin manifest, and skill manifest from Docker secrets and non-sensitive env vars.
  2. It writes `config.yml`, `models.yml`, and any other required config to the shared Docker volume at the path referenced by `PI_CONFIG_DIR`.
  3. It installs and pins the required OMP plugins and skills.
  4. It verifies that the configuration loads and that plugins/skills are discoverable.
  5. Handler containers start only after the init service reports healthy.
  6. Handlers mount the provisioned volume read-only at `PI_CONFIG_DIR` and set `PI_CODING_AGENT_DIR` to a writable per-handler location for runtime state.
- **Outcome:** Each handler has a consistent, read-only OMP environment without touching the host filesystem, and OMP can still write its runtime state.
- **Covers:** R1, R2, R3, R4, R5, R6, R14, R15, R16.

### F2. Fork-based PR creation and update

- **Trigger:** Forge executes a bead and has code changes to submit, or needs to update an existing PR.
- **Actors:** A3, A4, A5.
- **Steps:**
  1. Forge identifies the target repo, source branch, and bead identity.
  2. Using the workspace-org installation token, the system checks whether a fork exists and creates one if needed. Branch names are deterministic per bead/task.
  3. The execution branch is pushed to the workspace-org fork using the workspace-org token.
  4. The system checks for an existing open PR from the same fork branch; if none exists, it creates one on the source repo using the source-org token. If one exists, it updates the PR body/status as needed.
- **Outcome:** Source-org write access is limited; changes are proposed through standard PR review. Duplicate PRs are avoided.
- **Covers:** R7, R8, R9, R10, R11, R12.

### F3. PR event handling

- **Trigger:** GitHub sends a webhook event for a monitored PR.
- **Actors:** A7, A3.
- **Steps:**
  1. The getAlby/http-nostr bridge receives the payload over HTTPS and validates the HMAC-SHA256 signature using the webhook secret.
  2. It checks the event type against an allow-list and deduplicates using `X-GitHub-Delivery`.
  3. It drops events for out-of-scope repos.
  4. It converts the validated payload into a Nostr event and publishes it through the Pacto/Nostr daemon.
  5. Gate 3 receives the event as a `dm_received` or `mls_group_message_received` event and updates the proposal state (merged → closed, requested changes → return to Forge).
- **Outcome:** Proposal state advances through the existing Nostr event pipeline; fake or replayed events are rejected by signature validation and deduplication.
- **Covers:** R8, R13, R18, R19, R20, R21, R22.

---

## Scope Boundaries

- **Deferred for later:**
  - GitHub App Marketplace publishing.
  - OAuth-based user authorization flows.
  - Multi-organization deployments.
  - Branch-based PRs (changes pushed directly to the source repo).
  - Fine-grained per-handler credential scoping (e.g., only Forge gets GitHub keys).
  - Automated fork/branch cleanup policies and retention schedules.
  - Disaster recovery for merged PRs and image rebuilds.
  - Vercel/published-app ingress for the webhook bridge (ngrok is the v1 default).
- **Outside this product's identity:**
  - Modifying source-repo branch protection rules or CI/CD pipelines.
  - Acting as a human user rather than an automated bot identity.
  - Managing a machine user account (bot user) for fork ownership.

---

## Dependencies / Assumptions

- D1. The org has permission to install a GitHub App and add it to the target repos.
- D2. A dedicated workspace organization can be created for fork ownership.
- D3. The GitHub App can be installed on the workspace organization with permissions to create repositories and push branches.
- D4. OMP can be configured non-interactively from a generated config file, and plugins/skills can be installed non-interactively (the init service does not require a TUI).
- D5. The deployment network can reach the GitHub API and the configured LLM provider endpoints.
- D6. The GitHub App webhook endpoint is reachable from GitHub's infrastructure over HTTPS.
- D7. Private repos require a GitHub Team/Enterprise plan with cross-org forking enabled.
- D8. The workspace-org installation requires access to all repositories in the workspace org because fork names are derived from source repos and cannot be pre-enumerated.

---

## Outstanding Questions

### Resolve before planning

- Q1. Exact schema of `config.yml`, `models.yml`, and the plugin/skill manifest, including how `shipply.toml` model aliases map to OMP model roles and provider/model IDs.
- Q2. Whether to use a sidecar or init container for the OMP init service, and how it signals readiness to handlers.
- Q3. Exact layout of `PI_CODING_AGENT_DIR` runtime state directories per handler (ephemeral container writable layer, emptyDir volume, or named volume).

### Deferred to planning

- Q4. Rate-limiting and retry strategy for GitHub API calls across many repos.
- Q5. Bridge configuration: how the getAlby/http-nostr service learns the allowed repo list, event types, and Nostr routing (DM to Gate 3, MLS group, etc.).
- Q6. Fork/branch cleanup policy and retention schedule.
- Q7. Migration path from the current PAT-based GitHub auth to the GitHub App model.

---

## Sources / Research

- `src/shipply/harness.py` — `HarnessBackend` spawns `omp acp` and completes the ACP handshake; currently does not mount or pass provider credentials.
- `src/shipply/config.py` — `PersonaConfig` defines model aliases and `omp` invocation but does not wire them to the ACP session.
- `docker-compose.yml` — current handler services do not mount a shared OMP config volume; GitHub token is provided as a Docker secret only for `forge` and `gate3`.
- `shipply.toml.example` — current persona configuration uses abstract model names (`fast`, `default`, `slow`).
- `shipply-bots/docker-compose.yml` — Pacto bot daemon uses Unix socket and optional HTTP secret token transport.
- `shipply-bots/bots/*/README.md` — documents that external API keys are not managed by Pacto and should be set via environment variables.
- `Dockerfile` — `COPY --chown=botuser:botuser . .` copies the entire build context, including potential local secret files.
- `scripts/entrypoint-gh.sh` — reads a Docker secret and exports the token as `GH_TOKEN`/`GITHUB_TOKEN`.
- [OMP Environment variables](https://omp.sh/docs/env) — `PI_CONFIG_DIR`, `PI_CODING_AGENT_DIR`, and provider credential env vars.
- [OMP Secrets and auth](https://omp.sh/docs/secrets) — credential resolution order and the auth broker.
- [OMP ACP](https://omp.sh/docs/acp) — ACP mode reuses provider state under `~/.omp` and supports `session/set_model`.
- [OMP Model roles](https://omp.sh/docs/roles) — `smol`, `default`, `slow`, `plan` roles and `modelRoles` configuration.
- [OMP Providers](https://omp.sh/docs/providers) — OAuth, API keys, `models.yml`, and auth broker/gateway.
- [OMP Skills](https://omp.sh/docs/skills) — skill layout under `~/.omp/agent/skills/`.
- [OMP Plugins](https://omp.sh/docs/plugins) — `omp install` and plugin layout under `~/.omp/plugins/`.
