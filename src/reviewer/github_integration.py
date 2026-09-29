"""Minimal GitHub REST integration (needs GITHUB_TOKEN)."""
from __future__ import annotations

import os

import requests

API = "https://api.github.com"


def _headers(accept: str = "application/vnd.github+json") -> dict:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Set GITHUB_TOKEN to use GitHub features.")
    return {"Authorization": f"Bearer {token}", "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28"}


def fetch_pr(repo: str, number: int) -> dict:
    r = requests.get(f"{API}/repos/{repo}/pulls/{number}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_pr_diff(repo: str, number: int) -> str:
    r = requests.get(f"{API}/repos/{repo}/pulls/{number}",
                     headers=_headers("application/vnd.github.v3.diff"), timeout=30)
    r.raise_for_status()
    return r.text


def fetch_review_comments(repo: str, limit_prs: int = 30) -> list[str]:
    """Human review comments from the most recently closed PRs (bots skipped)."""
    r = requests.get(f"{API}/repos/{repo}/pulls",
                     params={"state": "closed", "per_page": limit_prs,
                             "sort": "updated", "direction": "desc"},
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    comments: list[str] = []
    for pr in r.json():
        cr = requests.get(f"{API}/repos/{repo}/pulls/{pr['number']}/comments",
                          params={"per_page": 100}, headers=_headers(), timeout=30)
        cr.raise_for_status()
        for c in cr.json():
            if c.get("user", {}).get("type") == "Bot":
                continue
            comments.append(c["body"])
    return comments


def post_review(repo: str, number: int, summary: str, findings: list[dict]) -> None:
    """Post findings as a single PR review (inline where a line is known)."""
    icons = {"blocker": "🛑", "major": "🔴", "minor": "🟡", "nit": "⚪"}

    def body(f: dict) -> str:
        text = f"{icons.get(f['severity'], '')} **{f['severity']}** · {f['category']}\n\n{f['message']}"
        if f.get("suggestion"):
            text += f"\n\n**Suggestion:** {f['suggestion']}"
        return text + f"\n\n<sub>finding #{f['id']}</sub>"

    inline = [{"path": f["file"], "line": f["line"], "side": "RIGHT", "body": body(f)}
              for f in findings if f.get("line")]
    general = [f"**{f['file']}**\n{body(f)}" for f in findings if not f.get("line")]
    review_body = summary + ("\n\n---\n\n" + "\n\n".join(general) if general else "")
    payload = {"body": review_body or "Automated review", "event": "COMMENT", "comments": inline}
    r = requests.post(f"{API}/repos/{repo}/pulls/{number}/reviews", json=payload,
                      headers=_headers(), timeout=30)
    r.raise_for_status()
