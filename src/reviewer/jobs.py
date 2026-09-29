"""Review jobs shared by the CLI and the webhook server."""
from __future__ import annotations

from . import github_integration as gh
from . import gitlab_integration as gl
from . import slack_integration as slack
from .agent import ReviewAgent, ReviewResult


def _notify(result: ReviewResult, title: str | None, url: str | None) -> None:
    try:
        slack.notify_review(result, title, url)
    except Exception as e:  # Slack must never break a review
        print(f"[warn] Slack notification failed: {e}")


def review_github_pr(agent: ReviewAgent, repo: str, number: int, post: bool = False,
                     notify: bool = False) -> ReviewResult:
    pr = gh.fetch_pr(repo, number)
    diff = gh.fetch_pr_diff(repo, number)
    res = agent.review(diff, pr.get("title"), pr.get("body"))
    if post and res.findings:
        gh.post_review(repo, number, res.summary, res.findings)
    if notify:
        _notify(res, pr.get("title"), pr.get("html_url"))
    return res


def review_gitlab_mr(agent: ReviewAgent, project: str | int, iid: int, post: bool = False,
                     notify: bool = False) -> ReviewResult:
    mr = gl.fetch_mr(project, iid)
    diff = gl.fetch_mr_diff(project, iid)
    res = agent.review(diff, mr.get("title"), mr.get("description"))
    if post and (res.findings or res.summary):
        gl.post_review(project, iid, res.summary, res.findings, mr)
    if notify:
        _notify(res, mr.get("title"), mr.get("web_url"))
    return res
