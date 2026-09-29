"""GitLab merge request integration (needs GITLAB_TOKEN; GITLAB_URL for self-hosted).

Environment:
    GITLAB_TOKEN  personal / project access token with `api` scope
    GITLAB_URL    default https://gitlab.com  (no trailing /api/v4)
"""
from __future__ import annotations

import os
from urllib.parse import quote

import requests

ICONS = {"blocker": "🛑", "major": "🔴", "minor": "🟡", "nit": "⚪"}


def _base() -> str:
    return os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/") + "/api/v4"


def _headers() -> dict:
    token = os.getenv("GITLAB_TOKEN")
    if not token:
        raise RuntimeError("Set GITLAB_TOKEN to use GitLab features.")
    return {"PRIVATE-TOKEN": token}


def _pid(project: str | int) -> str:
    """Project can be a numeric id or 'group/name' (must be URL-encoded)."""
    return quote(str(project), safe="")


def _get(path: str, **params):
    r = requests.get(f"{_base()}{path}", headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r


def changes_to_diff(changes: list[dict]) -> str:
    """Build one unified diff (git style) from GitLab's per-file diff objects."""
    chunks: list[str] = []
    for c in changes:
        if c.get("deleted_file") or not c.get("diff"):
            continue
        old = "/dev/null" if c.get("new_file") else f"a/{c['old_path']}"
        new = f"b/{c['new_path']}"
        chunks.append(f"diff --git a/{c['old_path']} b/{c['new_path']}\n"
                      f"--- {old}\n+++ {new}\n{c['diff'].rstrip(chr(10))}\n")
    return "".join(chunks)


def fetch_mr(project: str | int, iid: int) -> dict:
    return _get(f"/projects/{_pid(project)}/merge_requests/{iid}").json()


def fetch_mr_diff(project: str | int, iid: int) -> str:
    """Prefer the paginated /diffs endpoint; fall back to the older /changes."""
    changes: list[dict] = []
    try:
        page = 1
        while True:
            r = _get(f"/projects/{_pid(project)}/merge_requests/{iid}/diffs",
                     per_page=100, page=page)
            changes += r.json()
            if not r.headers.get("X-Next-Page"):
                break
            page += 1
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        changes = _get(f"/projects/{_pid(project)}/merge_requests/{iid}/changes").json()["changes"]
    return changes_to_diff(changes)


def _body(f: dict) -> str:
    text = f"{ICONS.get(f['severity'], '')} **{f['severity']}** ({f['category']})\n\n{f['message']}"
    if f.get("suggestion"):
        text += f"\n\n**Suggestion:** {f['suggestion']}"
    return text + f"\n\n_finding #{f['id']}_"


def post_review(project: str | int, iid: int, summary: str, findings: list[dict],
                mr: dict | None = None) -> None:
    """Post findings as inline discussions; anything that can't be placed goes in one note."""
    mr = mr or fetch_mr(project, iid)
    refs = mr.get("diff_refs") or {}
    base = f"{_base()}/projects/{_pid(project)}/merge_requests/{iid}"
    leftovers: list[dict] = []

    for f in findings:
        if not f.get("line") or not refs:
            leftovers.append(f)
            continue
        payload = {"body": _body(f), "position": {
            "position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
            "head_sha": refs["head_sha"], "old_path": f["file"], "new_path": f["file"],
            "new_line": f["line"]}}
        r = requests.post(f"{base}/discussions", json=payload, headers=_headers(), timeout=30)
        if r.status_code >= 400:  # line not commentable (e.g. renamed file) -> general note
            leftovers.append(f)

    note = summary or "Automated review"
    if leftovers:
        note += "\n\n---\n\n" + "\n\n".join(f"**{f['file']}**\n{_body(f)}" for f in leftovers)
    r = requests.post(f"{base}/notes", json={"body": note}, headers=_headers(), timeout=30)
    r.raise_for_status()


def fetch_review_comments(project: str | int, limit_mrs: int = 30) -> list[str]:
    """Human review comments from recently merged MRs (system notes and bots skipped)."""
    mrs = _get(f"/projects/{_pid(project)}/merge_requests", state="merged", per_page=limit_mrs,
               order_by="updated_at", sort="desc").json()
    comments: list[str] = []
    for m in mrs:
        notes = _get(f"/projects/{_pid(project)}/merge_requests/{m['iid']}/notes",
                     per_page=100).json()
        for n in notes:
            if n.get("system") or n.get("author", {}).get("bot"):
                continue
            comments.append(n["body"])
    return comments
