# workflows

Reusable GitHub Actions workflows for building Docker images and updating GitOps repositories.

---

## Workflows

### `build-push.yml`

Builds a Docker image and pushes it to a container registry using Azure OIDC (no stored credentials).

| Input | Required | Default | Description |
|---|---|---|---|
| `IMAGE_TAG` | yes | — | Tag to apply to the image |
| `REGISTRY_URL` | yes | — | Container registry hostname |
| `DOCKERFILE_PATH` | no | `./Dockerfile` | Path to Dockerfile |
| `BUILD_ARGS` | no | `""` | Space or newline-separated `KEY=value` build args |

| Secret | Required | Description |
|---|---|---|
| `AZURE_CLIENT_ID` | yes | App Registration client ID (OIDC) |
| `AZURE_TENANT_ID` | yes | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | yes | Azure subscription ID |
| `GITHUB_PAT` | no | PAT for private submodules |

**Output:** `image_tag` — the tag that was pushed.

The caller job must declare `permissions: id-token: write` for OIDC to work.

---

### `update-gitops.yml`

Updates `image.repository` and `image.tag` in a Helm values file in a GitOps repo, then commits and pushes. Includes retry logic (up to 5 attempts) to handle concurrent pushes from multiple services.

| Input | Required | Default | Description |
|---|---|---|---|
| `IMAGE_TAG` | yes | — | Tag to write into the values file |
| `REGISTRY_URL` | yes | — | Container registry hostname |
| `ENVIRONMENT` | no | `""` | Target environment (`dev`, `stage`, `prod`). When omitted, uses a flat path. |
| `VALUES_PATH_PREFIX` | no | `aks-baip` | Root folder inside the GitOps repo containing values files |
| `GITOPS_REPO` | no | `BaiP-AI-Private/baip-argocd-helm` | GitOps repository (`org/repo`) |
| `GITOPS_BRANCH` | no | `main` | Branch to commit to |
| `AUTH_METHOD` | no | `pat` | Auth method for GitOps repo: `pat` or `deploy_key` (see below) |
| `AZURE_KEYVAULT_NAME` | no | `baip-kv` | Key Vault name (deploy_key mode only) |
| `AZURE_KEYVAULT_SECRET_NAME` | no | `baip-argocd-helm-deploy-key` | Secret name containing the SSH deploy key (deploy_key mode only) |

| Secret | Required for | Description |
|---|---|---|
| `GITHUB_PAT` | `pat` mode | PAT with write access to the GitOps repo |
| `AZURE_CLIENT_ID` | `deploy_key` mode | App Registration client ID (OIDC) |
| `AZURE_TENANT_ID` | `deploy_key` mode | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | `deploy_key` mode | Azure subscription ID |

**Values file path:**

```
# With ENVIRONMENT set:
{VALUES_PATH_PREFIX}/{environment}/{repo-name}.yaml

# Without ENVIRONMENT (single-env):
{VALUES_PATH_PREFIX}/{repo-name}.yaml
```

The values file must exist before the first run — it will not be auto-created.

---

## Usage

Copy `example_usecase/ci-cd.yml` into your application repo as `.github/workflows/ci-cd.yml`.
No edits required — the service name is auto-derived from `github.event.repository.name`.

### Trigger → environment mapping

| Trigger | Image tag | Target environment |
|---|---|---|
| Push to `dev` branch | `{short-sha}-{date}` | `dev` |
| Push `v*.*.*` tag | `v1.0.0` | `stage` |
| Publish GitHub Release | reuses tag image, no rebuild | `prod` |
| Push to `main` | — | no workflow runs |

### Authentication methods

Two options are available for authenticating the GitOps repo push:

#### Option A: GitHub PAT (default)

Uses a personal access token stored as an org secret. No extra permissions needed on the caller job.

```yaml
deploy:
  permissions:
    contents: read
  uses: BaiP-AI-Private/baip-workflows/.github/workflows/update-gitops.yml@main
  with:
    IMAGE_TAG:    ${{ needs.determine-context.outputs.image_tag }}
    ENVIRONMENT:  ${{ needs.determine-context.outputs.environment }}
    REGISTRY_URL: baipacr.azurecr.io
    # AUTH_METHOD defaults to 'pat'
  secrets: inherit
```

**Required org secret:** `GITHUB_PAT` with write access to `baip-argocd-helm`.

#### Option B: Azure Key Vault deploy key (no PAT needed)

Fetches an SSH deploy key from Azure Key Vault using the existing OIDC app (`baip-github-actions`). The deploy key is scoped to `baip-argocd-helm` only. The caller job must declare `id-token: write` for OIDC to work.

```yaml
deploy:
  permissions:
    contents: read
    id-token: write   # required for Azure OIDC login
    issues: write     # required for prod deployment tracking issue
  uses: BaiP-AI-Private/baip-workflows/.github/workflows/update-gitops.yml@main
  with:
    IMAGE_TAG:    ${{ needs.determine-context.outputs.image_tag }}
    ENVIRONMENT:  ${{ needs.determine-context.outputs.environment }}
    REGISTRY_URL: baipacr.azurecr.io
    AUTH_METHOD:  deploy_key
  secrets: inherit
```

**Required org secrets:** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.
The OIDC app must have `Key Vault Secrets User` role scoped to the `baip-argocd-helm-deploy-key` secret in `baip-kv`.

### Required secrets

| Secret | Auth method | Description |
|---|---|---|
| `AZURE_CLIENT_ID` | both | App Registration client ID (OIDC) |
| `AZURE_TENANT_ID` | both | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | both | Azure subscription ID |
| `GITHUB_PAT` | `pat` only | PAT with write access to the GitOps repo |

### Passing Docker build arguments

```yaml
build:
  with:
    REGISTRY_URL: myregistry.azurecr.io
    IMAGE_TAG: ${{ needs.determine-context.outputs.image_tag }}
    BUILD_ARGS: |
      APP_VERSION=${{ needs.determine-context.outputs.image_tag }}
      NODE_ENV=production
  secrets: inherit
```
