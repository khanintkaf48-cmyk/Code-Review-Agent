"""Command line interface: `reviewer <command>`."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .agent import ReviewAgent, ReviewResult
from .config import Config
from .diff_utils import git_diff
from .memory import VALID_STATUSES

ICONS = {"blocker": "[BLOCKER]", "major": "[MAJOR]  ", "minor": "[minor]  ", "nit": "[nit]    "}


def print_result(res: ReviewResult) -> None:
    print(f"\nReview {res.review_id}\n{'=' * 40}")
    print(res.summary or "(no summary)")
    if res.truncated:
        print("\n! Diff was too large and got truncated; only the first part was reviewed.")
    if not res.findings:
        print("\nNo new findings. 🎉")
    for f in res.findings:
        loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
        print(f"\n#{f['id']} {ICONS.get(f['severity'], '')} {loc}  ({f['category']})")
        print(f"  {f['message']}")
        if f.get("previously_rejected"):
            print("  ! Rejected before, shown again because it is a blocker.")
        if f.get("suggestion"):
            print(f"  -> {f['suggestion']}")
    if res.suppressed:
        print(f"\n({len(res.suppressed)} suggestion(s) hidden because your team rejected them before)")
    if res.findings:
        print("\nTeach the agent:  reviewer feedback <id> accepted|rejected [--reason '...']")


def cmd_review(agent: ReviewAgent, args) -> int:
    from . import jobs

    if args.pr:
        if not args.repo:
            sys.exit("--repo owner/name is required with --pr")
        res = jobs.review_github_pr(agent, args.repo, args.pr, post=args.post, notify=args.slack)
    elif args.mr:
        if not args.gitlab_project:
            sys.exit("--gitlab-project group/name (or numeric id) is required with --mr")
        res = jobs.review_gitlab_mr(agent, args.gitlab_project, args.mr, post=args.post,
                                    notify=args.slack)
    else:
        if args.diff_file:
            diff = sys.stdin.read() if args.diff_file == "-" else open(args.diff_file, encoding="utf-8").read()
        else:
            diff = git_diff(base=args.base, staged=args.staged)
        res = agent.review(diff)
        if args.slack:
            from . import slack_integration as slack
            slack.notify_review(res)

    if args.json:
        print(json.dumps({"review_id": res.review_id, "summary": res.summary,
                          "findings": res.findings}, indent=2))
    else:
        print_result(res)
    if args.post and (args.pr or args.mr) and res.findings:
        print("\nPosted review.")
    return 1 if args.fail_on_blocker and any(f["severity"] == "blocker" for f in res.findings) else 0


def cmd_feedback(agent: ReviewAgent, args) -> int:
    if not agent.memory.set_status(args.finding_id, args.status, args.reason):
        print(f"No finding with id {args.finding_id}")
        return 1
    msg = f"Recorded: finding #{args.finding_id} -> {args.status}."
    if args.status == "rejected":
        msg += " I won't raise this again" + (" and saved your reason as a team rule." if args.reason else ".")
    print(msg)
    return 0


def cmd_teach(agent: ReviewAgent, args) -> int:
    rid = agent.memory.add_rule(args.rule, args.category, args.language, source="manual")
    print("Rule added." if rid else "Rule already known (reinforced).")
    return 0


def cmd_learn(agent: ReviewAgent, args) -> int:
    if args.repo:
        from . import github_integration as gh
        comments = gh.fetch_review_comments(args.repo, args.prs)
    elif args.gitlab_project:
        from . import gitlab_integration as gl
        comments = gl.fetch_review_comments(args.gitlab_project, args.prs)
    elif args.file:
        text = open(args.file, encoding="utf-8").read()
        try:
            data = json.loads(text)
            comments = [c if isinstance(c, str) else c.get("body", "") for c in data]
        except json.JSONDecodeError:
            comments = [l for l in text.splitlines() if l.strip()]
    else:
        sys.exit("Give --repo owner/name, --gitlab-project group/name or --file comments.(json|txt)")
    print(f"Analyzing {len(comments)} past review comments...")
    added = agent.learn_from_comments(comments, source="history")
    for r in added:
        print(f"  + [{r.get('category')}] {r.get('text')}")
    print(f"Learned {len(added)} new rule(s).")
    return 0


def cmd_rules(agent: ReviewAgent, args) -> int:
    if args.delete:
        print("Deleted." if agent.memory.delete_rule(args.delete) else "No such rule.")
        return 0
    for r in agent.memory.list_rules():
        print(f"#{r['id']:<3} [{r['category']}|{r['language']}|{r['source']}] {r['text']}")
    return 0


def cmd_stats(agent: ReviewAgent, args) -> int:
    s = agent.memory.stats()
    rate = f"{s['acceptance_rate']:.0%}" if s["acceptance_rate"] is not None else "n/a"
    print(f"Rules learned: {s['rules']}\nFindings made: {s['findings']} "
          f"(accepted {s['accepted']}, rejected {s['rejected']}, open {s['open']})\n"
          f"Acceptance rate: {rate}\nDistinct patterns: {s['patterns']}")
    top = agent.memory.frequent_mistakes(5)
    if top:
        print("\nMost common real mistakes:")
        for m in top:
            print(f"  - {m['pattern_key']} (accepted {m['accepted']}x)")
    return 0


def cmd_serve(_agent, args) -> int:
    from .server import serve
    return serve(args.host, args.port, demo=args.demo, verbose=args.verbose,
                 open_browser=args.open)


def cmd_demo(_agent, _args) -> int:
    from .demo import run_demo
    run_demo()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reviewer", description="Code review agent that learns your team.")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("review", help="review a diff")
    r.add_argument("--base", help="compare against this branch (git diff BASE...HEAD)")
    r.add_argument("--staged", action="store_true", help="review staged changes")
    r.add_argument("--diff-file", help="path to a .diff file, or - for stdin")
    r.add_argument("--repo", help="GitHub repo owner/name")
    r.add_argument("--pr", type=int, help="GitHub PR number")
    r.add_argument("--gitlab-project", help="GitLab project (group/name or numeric id)")
    r.add_argument("--mr", type=int, help="GitLab merge request iid")
    r.add_argument("--post", action="store_true", help="post findings back to the PR / MR")
    r.add_argument("--slack", action="store_true", help="also notify Slack (SLACK_WEBHOOK_URL)")
    r.add_argument("--json", action="store_true", help="machine readable output")
    r.add_argument("--fail-on-blocker", action="store_true", help="exit 1 if a blocker is found (CI)")
    r.set_defaults(fn=cmd_review)

    f = sub.add_parser("feedback", help="tell the agent if a finding was right")
    f.add_argument("finding_id", type=int)
    f.add_argument("status", choices=sorted(VALID_STATUSES))
    f.add_argument("--reason", help="why (saved as a team rule when rejecting)")
    f.set_defaults(fn=cmd_feedback)

    t = sub.add_parser("teach", help="add a team rule manually")
    t.add_argument("rule")
    t.add_argument("--category", default="convention")
    t.add_argument("--language", default="*")
    t.set_defaults(fn=cmd_teach)

    l = sub.add_parser("learn", help="learn rules from past review comments")
    l.add_argument("--repo", help="GitHub repo owner/name")
    l.add_argument("--gitlab-project", help="GitLab project (group/name or numeric id)")
    l.add_argument("--prs", type=int, default=30, help="how many recent closed PRs/MRs to scan")
    l.add_argument("--file", help="JSON list or text file of comments")
    l.set_defaults(fn=cmd_learn)

    ru = sub.add_parser("rules", help="list learned rules")
    ru.add_argument("--delete", type=int, metavar="ID")
    ru.set_defaults(fn=cmd_rules)

    sub.add_parser("stats", help="show what the agent has learned").set_defaults(fn=cmd_stats)

    sv = sub.add_parser("serve", help="dashboard + API + webhooks (GitHub, GitLab, Slack)")
    sv.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to expose (set REVIEWER_DASHBOARD_TOKEN!)")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--demo", action="store_true", help="offline demo model + sample data (no API key)")
    sv.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    sv.add_argument("--verbose", action="store_true")
    sv.set_defaults(fn=cmd_serve)

    sub.add_parser("demo", help="offline walkthrough of the learning loop (no API key)").set_defaults(fn=cmd_demo)
    return p


def _setup_console() -> None:
    """Make output safe on Windows consoles (symbols, colours)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        os.system("")  # switches on ANSI colour support in the Windows console


def main(argv: list[str] | None = None) -> None:
    _setup_console()
    args = build_parser().parse_args(argv)
    agent = None if args.command in ("serve", "demo") else ReviewAgent(Config())
    try:
        code = args.fn(agent, args)
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # noqa: BLE001 - show a readable message, not a traceback
        if os.getenv("REVIEWER_DEBUG"):
            raise
        print(f"\nError: {e}", file=sys.stderr)
        code = 1
    finally:
        if agent is not None:
            agent.memory.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
