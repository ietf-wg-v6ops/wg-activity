# ietf-kanban

Automatically syncs IETF Datatracker documents for the [v6ops working group](https://datatracker.ietf.org/wg/v6ops/documents/) into the [GitHub Projects v2 Kanban board](https://github.com/orgs/ietf-wg-v6ops/projects/1).

## How it works

1. Loads all Datatracker state slugs (`wg-doc`, `wg-lc`, `ad-eval`, `rfcqueue`, etc.)
2. Fetches all `draft-ietf-v6ops-*` documents from the Datatracker REST API
3. Maps each document's state to a Kanban column
4. Creates or moves cards in GitHub Projects v2 via the GraphQL API

The workflow runs daily at 06:00 UTC and can also be triggered manually.

## Column mapping

| Column | Datatracker States |
|---|---|
| New | `wg-cand`, `c-adopt`, `adopt-wg` |
| Active | `wg-doc`, `writeupw`, `chair-w` |
| WG Last Call | `wg-lc`, `held-by-wg` |
| IESG / AD | `sub-pub`, `ad-eval`, `iesg-rev`, `lc`, … |
| RFC Editor Queue | `rfcqueue`, `missref`, `auth48` |
| Published RFC | `rfc_number` set |
| Parked / Expired | `parked`, `dead`, past `expires` date |

## One-time setup

### 1. Create the Status field in GitHub Projects

Go to the project settings at:
```
https://github.com/orgs/ietf-wg-v6ops/projects/1/settings
```

Add a **Single select** field named exactly `Status` with these options (in order):

- `New`
- `Active`
- `WG Last Call`
- `IESG / AD`
- `RFC Editor Queue`
- `Published RFC`
- `Parked / Expired`

The column names must match exactly — the sync script looks them up by name.

### 2. Create a Personal Access Token

Create a PAT (classic or fine-grained) with the following scopes:

- `project` — read/write access to GitHub Projects
- `read:org` — read org-level project metadata

### 3. Add the token as a repository secret

In this repo go to **Settings → Secrets and variables → Actions** and add:

| Name | Value |
|---|---|
| `PROJECT_TOKEN` | the PAT from step 2 |

### 4. Push this repo to the ietf-wg-v6ops org

The workflow must live inside a repo that belongs to (or has access to) the `ietf-wg-v6ops` organization.

```bash
git init
git remote add origin git@github.com:ietf-wg-v6ops/kanban.git
git add .
git commit -m "Initial kanban sync setup"
git push -u origin main
```

### 5. Run the workflow

Trigger an initial sync manually:

```
GitHub Actions → Sync Datatracker → GitHub Project → Run workflow
```

Subsequent runs happen automatically every day at 06:00 UTC.

## Manual / local usage

```bash
pip install requests

export GITHUB_TOKEN=<your-pat>
export GITHUB_ORG=ietf-wg-v6ops
export PROJECT_NUMBER=1
export WG_ACRONYM=v6ops

python scripts/sync_datatracker.py
```

## Files

```
.github/workflows/sync-datatracker.yml   # GitHub Actions workflow (daily cron + manual dispatch)
scripts/sync_datatracker.py              # sync logic
```
