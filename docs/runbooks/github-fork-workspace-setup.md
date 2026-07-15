# GitHub fork workspace setup

This runbook records the decision and operational steps for using a dedicated workspace organization to host forks of the source organization's repositories.

## Decision

- **Decision ID:** `shipcrew-3qf.9`
- **Date:** 2026-07-15
- **Decision:** Proceed with fork-based PRs from a dedicated workspace organization. Verify that the workspace organization can fork the source organization's repositories before the deployment is considered healthy, and defer a branch-based fallback to a follow-up plan.
- **Rationale:**
  - Fork-based PRs minimize the write permissions required on the source organization. The source-org GitHub App installation only needs `contents:read`, `pull_requests:write`, and `metadata:read`.
  - A dedicated workspace organization isolates bot-created forks and branches from the source org's repository namespace.
  - Branch-based PRs would require `contents:write` on the source org and complicate branch-protection rules; they are therefore deferred to a later plan if cross-org forking cannot be enabled.
- **Runbook:** This document.
- **Related plan:** `docs/plans/2026-07-14-002-feat-shared-omp-config-and-github-app-plan.md`

## Required GitHub plan and permissions

### GitHub plan requirements

- The source organization must be on a GitHub plan that allows private repository forks to another organization. **GitHub Free does not allow forking a private repository to an organization**.
- The workspace organization must be on GitHub Team, GitHub Enterprise Cloud, or GitHub Enterprise Server.

### Organization forking policy

1. In the **source organization**, enable forking of private repositories:
   - Organization settings → Repository forking → **Allow forking of private repositories**.
2. In the **workspace organization**, enable creation of private forks:
   - Organization settings → Repository forking → **Allow forking of private and internal repositories**.
3. If the source organization is owned by an enterprise account, the enterprise fork policy must allow forks to the workspace organization or to any organization.

### GitHub App permissions

Create or reuse a single GitHub App and install it on **both** organizations.

- **Source organization installation**
  - Repository permissions:
    - `contents:read`
    - `pull_requests:write`
    - `metadata:read`
  - Subscribe to events:
    - `pull_request`
    - `pull_request_review`
  - Scope the installation to the repositories Shipply should act on.

- **Workspace organization installation**
  - Repository permissions:
    - `administration:write` (required to create repositories/forks)
    - `contents:write` (required to push branches)
  - Grant access to **all repositories** in the workspace organization, because forks are created on demand.

## Steps to create the workspace org and install the GitHub App

1. **Create the workspace organization**
   - Choose a name (e.g., `your-org-shipply-workspace`).
   - Set the org visibility to match your policy; the forks themselves inherit the source repo visibility.
   - Restrict membership to operators and the bot identity.

2. **Create the GitHub App**
   - Developer settings → GitHub Apps → New GitHub App.
   - Fill in name, description, and homepage URL.
   - Turn off interactive user OAuth flows (this is an internal bot app).
   - Set webhook URL and secret (see the webhook bridge runbook in U6).
   - Set the permissions listed above.
   - Subscribe to `pull_request` and `pull_request_review` events.
   - Generate a private key and download the `.pem` file. This is mounted as the Docker secret `github-app-private-key`.

3. **Install the GitHub App on the source organization**
   - Install the app on the source org.
   - Select **Only select repositories** and choose the repos Shipply should serve.
   - Record the installation ID from the URL (`/installations/{installation_id}`).

4. **Install the GitHub App on the workspace organization**
   - Install the app on the workspace org.
   - Select **All repositories**.
   - Record the installation ID.

5. **Configure `shipply.toml`**

   ```toml
   [github]
   source_org = "your-source-org"
   workspace_org = "your-org-shipply-workspace"
   app_id = 123456
   source_installation_id = 11111111
   workspace_installation_id = 22222222
   repo_scope = ["source-repo-1", "source-repo-2"]
   ```

6. **Store the private key and webhook secret as Docker secrets**
   - Place the `.pem` file at `secrets/github-app-private-key.pem`.
   - Place the webhook secret at `secrets/github-app-webhook-secret`.
   - Ensure `secrets/` and `*.pem` are excluded by `.gitignore` and `.dockerignore`.

## How to verify cross-org private fork capability

The deployment should run a pre-flight check before any handler is considered healthy. The check validates:

1. The source organization is reachable.
2. The workspace organization is reachable.
3. The workspace installation token has the `administration:write` permission.
4. A sample fork from the source org to the workspace org can be created (or already exists).
5. If the source repository is private, the fork in the workspace org is also private.

### Manual verification with curl

1. Obtain a workspace-org installation token. In production this is provided by the token manager; for manual verification you can generate one with the GitHub App private key.
2. Verify the workspace org:

   ```bash
   curl -H "Authorization: Bearer $WORKSPACE_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/orgs/{workspace_org}
   ```

3. Verify the source repo is reachable:

   ```bash
   curl -H "Authorization: Bearer $WORKSPACE_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/repos/{source_org}/{repo}
   ```

4. List the installation repositories and check permissions:

   ```bash
   curl -H "Authorization: Bearer $WORKSPACE_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/installation/repositories
   ```

   Confirm the response includes `permissions.administration: "write"`.

5. Try to create the fork:

   ```bash
   curl -X POST \
        -H "Authorization: Bearer $WORKSPACE_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/repos/{source_org}/{repo}/forks \
        -d '{"organization":"{workspace_org}","name":"{repo}","default_branch_only":true}'
   ```

   A `202 Accepted` response means the fork is being created. A `403` typically means the organization forking policy or App permission is missing. A `404` means the source repo is not accessible to the App installation.

6. Verify the fork visibility:

   ```bash
   curl -H "Authorization: Bearer $WORKSPACE_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/repos/{workspace_org}/{repo}
   ```

   If the source repo is private, confirm `"private": true` in the response.

## How to run the pre-flight check

The pre-flight check is implemented in `src/shipply/github_preflight.py` and can be run as a module:

```bash
export GITHUB_TOKEN="<workspace-org installation token>"
python -m shipply.github_preflight
```

The command reads `shipply.toml` from the current directory (or `SHIPPLY_CONFIG`), validates the fork capability, and prints a result:

- Exit code `0` and a success message if the workspace org can fork the source repo.
- Exit code `1` and an actionable error message if any check fails.

In a container, the token manager can vend the workspace token to the pre-flight check before the handler services start. For example, in a startup script:

```bash
WORKSPACE_TOKEN=$(python -c 'from shipply.github_app import GitHubAppTokenManager; ...')
export GITHUB_TOKEN="$WORKSPACE_TOKEN"
python -m shipply.github_preflight
```

If the pre-flight check fails, do not start the handlers. Fix the organization policy, GitHub App permissions, or repository scope and re-run the check.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `404` from source repo | GitHub App not installed on source org or repo not in scope | Add the repo to the source-org installation. |
| `403` on fork creation | Workspace org forbids private forks, or App lacks `administration:write` | Enable forking in workspace org settings and verify App permissions. |
| Public fork for private source | Workspace or enterprise fork policy forces public forks | Set the policy to allow private forks to the workspace org. |
| `401` from GitHub API | Installation token expired or invalid | Re-issue the token from the token manager. |

## Deferral note

A branch-based fallback (pushing branches directly to the source organization) is intentionally deferred. If the fork model cannot be enabled for a given repository, the follow-up plan must address the additional source-org write permissions, branch protection exceptions, and idempotency logic required for that fallback.
