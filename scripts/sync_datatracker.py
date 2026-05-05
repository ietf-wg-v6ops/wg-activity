#!/usr/bin/env python3
"""
Sync IETF Datatracker v6ops documents to a GitHub Projects v2 board.

Required env vars:
  GITHUB_TOKEN   - PAT with 'project' and 'read:org' scopes
  GITHUB_ORG     - org login (default: ietf-wg-v6ops)
  PROJECT_NUMBER - project number (default: 1)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

DATATRACKER_BASE = "https://datatracker.ietf.org/api/v1"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_ORG = os.environ.get("GITHUB_ORG", "ietf-wg-v6ops")
PROJECT_NUMBER = int(os.environ.get("PROJECT_NUMBER", "1"))
WG_ACRONYM = os.environ.get("WG_ACRONYM", "v6ops")

# Datatracker state-slug → Kanban column name
STATE_COLUMN_MAP = {
    # Adoption / early
    "wg-cand":   "New",
    "c-adopt":   "New",
    "adopt-wg":  "New",
    "info":      "Active",
    # Active WG work
    "wg-doc":    "Active",
    "chair-w":   "Active",
    "writeupw":  "Active",
    # WG Last Call
    "wg-lc":                     "WG Last Call",
    "waiting-for-implementation": "WG Last Call",
    "held-by-wg":                "WG Last Call",
    # IESG / AD pipeline
    "sub-pub":   "IESG / AD",
    "pub-req":   "IESG / AD",
    "ad-eval":   "IESG / AD",
    "review-e":  "IESG / AD",
    "lc-cands":  "IESG / AD",
    "lc-req":    "IESG / AD",
    "lc":        "IESG / AD",
    "iesg-eva":  "IESG / AD",
    "iesg-rev":  "IESG / AD",
    "defer":     "IESG / AD",
    "goahead":   "IESG / AD",
    "ann":       "IESG / AD",
    # RFC Editor queue
    "rfcqueue":  "RFC Editor Queue",
    "missref":   "RFC Editor Queue",
    "iana":      "RFC Editor Queue",
    "ref":       "RFC Editor Queue",
    "auth48":    "RFC Editor Queue",
    "auth48-done": "RFC Editor Queue",
    # Dead / parked
    "parked":    "Parked / Expired",
    "dead":      "Parked / Expired",
    "nopubadw":  "Parked / Expired",
    "nopubanw":  "Parked / Expired",
}

KANBAN_COLUMNS = [
    "New",
    "Active",
    "WG Last Call",
    "IESG / AD",
    "RFC Editor Queue",
    "Published RFC",
    "Parked / Expired",
]


# ---------------------------------------------------------------------------
# Datatracker helpers
# ---------------------------------------------------------------------------

def dt_get(path: str, params: dict = None) -> dict:
    url = f"{DATATRACKER_BASE}{path}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load_state_map() -> dict[str, str]:
    """Return {state_resource_uri: slug} for all draft states."""
    state_map = {}
    for type_slug in ("draft-stream-ietf", "draft-iesg", "draft-rfceditor", "draft-iana"):
        offset = 0
        while True:
            data = dt_get("/doc/state/", {"type__slug": type_slug, "format": "json",
                                           "limit": 100, "offset": offset})
            for s in data.get("objects", []):
                state_map[s["resource_uri"]] = s["slug"]
            if not data.get("meta", {}).get("next"):
                break
            offset += 100
    return state_map


def fetch_wg_drafts(wg: str) -> list[dict]:
    """Fetch all draft documents for a WG from Datatracker (handles pagination)."""
    docs = []
    offset = 0
    while True:
        data = dt_get("/doc/document/", {
            "group__acronym": wg,
            "type": "draft",
            "format": "json",
            "limit": 100,
            "offset": offset,
        })
        docs.extend(data.get("objects", []))
        log.info("Fetched %d/%d documents", len(docs), data["meta"]["total_count"])
        if not data["meta"].get("next"):
            break
        offset += 100
        time.sleep(0.5)
    return docs


def resolve_column(doc: dict, state_map: dict[str, str]) -> str:
    """Map a Datatracker document to a Kanban column name."""
    # Published RFC
    if doc.get("rfc") or doc.get("rfc_number"):
        return "Published RFC"

    # Expired (past expiry date and no active IESG/RFC state)
    expires_str = doc.get("expires")
    if expires_str:
        expires = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        if expires < datetime.now(timezone.utc):
            # Only mark expired if not in an active IESG/RFC pipeline state
            active_pipeline = {
                "sub-pub", "pub-req", "ad-eval", "review-e", "lc-cands", "lc-req",
                "lc", "iesg-eva", "iesg-rev", "defer", "goahead", "ann",
                "rfcqueue", "missref", "iana", "ref", "auth48", "auth48-done",
            }
            doc_slugs = {state_map.get(s, "") for s in doc.get("states", [])}
            if not doc_slugs & active_pipeline:
                return "Parked / Expired"

    # Walk states in priority order (most-progressed wins)
    priority = list(STATE_COLUMN_MAP.keys())
    best_col = "Active"
    best_idx = len(priority)
    for state_uri in doc.get("states", []):
        slug = state_map.get(state_uri, "")
        if slug in STATE_COLUMN_MAP:
            col = STATE_COLUMN_MAP[slug]
            idx = priority.index(slug)
            if idx < best_idx:
                best_idx = idx
                best_col = col
    return best_col


def doc_body(doc: dict) -> str:
    parts = []
    if doc.get("abstract"):
        parts.append(doc["abstract"].strip())
    parts.append(f"Pages: {doc.get('pages', '?')}")
    if doc.get("expires"):
        parts.append(f"Expires: {doc['expires'][:10]}")
    rev = doc.get("rev", "")
    parts.append(f"Revision: {rev}")
    parts.append(f"Datatracker: https://datatracker.ietf.org/doc/{doc['name']}/")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# GitHub Projects v2 GraphQL helpers
# ---------------------------------------------------------------------------

def gh_graphql(query: str, variables: dict = None, retries: int = 5) -> dict:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(1, retries + 1):
        resp = requests.post(GITHUB_GRAPHQL, json=payload, headers=headers, timeout=30)
        if resp.status_code in (502, 503, 504) and attempt < retries:
            wait = 2 ** attempt
            log.warning("GitHub API %s (attempt %d/%d), retrying in %ds…",
                        resp.status_code, attempt, retries, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
        return data["data"]


def get_project_meta(org: str, project_number: int) -> dict:
    """Return project id, status field id, and option-name→id map."""
    query = """
    query($org: String!, $number: Int!) {
      organization(login: $org) {
        projectV2(number: $number) {
          id
          fields(first: 30) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id
                name
                options { id name }
              }
            }
          }
        }
      }
    }
    """
    data = gh_graphql(query, {"org": org, "number": project_number})
    project = data["organization"]["projectV2"]
    project_id = project["id"]

    status_field_id = None
    option_map = {}
    for field in project["fields"]["nodes"]:
        if field.get("name") == "Status":
            status_field_id = field["id"]
            option_map = {opt["name"]: opt["id"] for opt in field.get("options", [])}
            break

    return {
        "project_id": project_id,
        "status_field_id": status_field_id,
        "option_map": option_map,
    }


def ensure_status_options(meta: dict, required_columns: list[str]) -> dict:
    """
    Warn about missing Status options (columns must be created manually in
    the GitHub Projects UI — the API does not support adding options).
    Returns updated option_map.
    """
    missing = [c for c in required_columns if c not in meta["option_map"]]
    if missing:
        log.warning(
            "The following Status columns are missing from the GitHub Project "
            "and must be created manually in the UI: %s",
            missing,
        )
    return meta["option_map"]


def get_existing_items(org: str, project_number: int) -> dict[str, str]:
    """Return {draft-name: item_id} for all items currently in the project."""
    query = """
    query($org: String!, $number: Int!, $cursor: String) {
      organization(login: $org) {
        projectV2(number: $number) {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                ... on DraftIssue { title }
                ... on Issue { title }
              }
            }
          }
        }
      }
    }
    """
    items = {}
    cursor = None
    while True:
        data = gh_graphql(query, {"org": org, "number": project_number, "cursor": cursor})
        page = data["organization"]["projectV2"]["items"]
        for node in page["nodes"]:
            content = node.get("content") or {}
            title = content.get("title", "")
            if title.startswith("draft-"):
                items[title] = node["id"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return items


def add_draft_item(project_id: str, title: str, body: str) -> str:
    """Add a draft issue to the project; return the new item id."""
    mutation = """
    mutation($projectId: ID!, $title: String!, $body: String!) {
      addProjectV2DraftIssue(input: {
        projectId: $projectId
        title: $title
        body: $body
      }) {
        projectItem { id }
      }
    }
    """
    data = gh_graphql(mutation, {
        "projectId": project_id,
        "title": title,
        "body": body,
    })
    return data["addProjectV2DraftIssue"]["projectItem"]["id"]


def set_item_status(project_id: str, item_id: str, field_id: str, option_id: str) -> None:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) {
        projectV2Item { id }
      }
    }
    """
    gh_graphql(mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "optionId": option_id,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Loading Datatracker state map…")
    state_map = load_state_map()
    log.info("Loaded %d state entries", len(state_map))

    log.info("Fetching v6ops drafts from Datatracker…")
    docs = fetch_wg_drafts(WG_ACRONYM)
    log.info("Found %d total drafts", len(docs))

    # Exclude unrelated docs (sanity check: must be ietf-wg-v6ops)
    docs = [d for d in docs if f"draft-ietf-{WG_ACRONYM}-" in d["name"]
            or d["name"].startswith(f"draft-{WG_ACRONYM}-")]

    log.info("Syncing %d WG drafts to GitHub Project %s#%d…",
             len(docs), GITHUB_ORG, PROJECT_NUMBER)

    meta = get_project_meta(GITHUB_ORG, PROJECT_NUMBER)
    log.info("Project ID: %s", meta["project_id"])
    log.info("Status options: %s", list(meta["option_map"].keys()))

    option_map = ensure_status_options(meta, KANBAN_COLUMNS)
    existing = get_existing_items(GITHUB_ORG, PROJECT_NUMBER)
    log.info("Existing project items: %d", len(existing))

    created = updated = skipped = 0

    for doc in docs:
        name = doc["name"]
        title_ver = f"{name}-{doc.get('rev', '00')}"
        column = resolve_column(doc, state_map)
        option_id = option_map.get(column)

        if option_id is None:
            log.warning("No option_id for column '%s' (doc: %s) — skipping status update", column, name)

        # Use the base draft name as the canonical title so we don't
        # create duplicate cards when the revision bumps.
        item_id = existing.get(name)
        if item_id is None:
            log.info("  + Creating: %s → %s", name, column)
            body = doc_body(doc)
            item_id = add_draft_item(meta["project_id"], name, body)
            existing[name] = item_id
            created += 1
        else:
            log.info("  ~ Updating: %s → %s", name, column)
            updated += 1

        if option_id and meta["status_field_id"]:
            set_item_status(
                meta["project_id"],
                item_id,
                meta["status_field_id"],
                option_id,
            )
        else:
            skipped += 1

        time.sleep(0.2)  # be polite to the GitHub API

    log.info("Done. Created: %d  Updated: %d  Skipped (no column): %d",
             created, updated, skipped)


if __name__ == "__main__":
    main()
