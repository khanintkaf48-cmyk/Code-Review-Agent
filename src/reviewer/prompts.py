"""Prompt construction. All memory gets injected here."""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior code reviewer embedded in a specific engineering team. You are NOT a \
generic linter: you review against THIS team's standards, and you know which mistakes \
THIS team tends to make.

How to review:
- Focus on real problems: bugs, security issues, error handling, concurrency, data loss, \
performance traps, architectural violations, and breaches of the team's rules below.
- Do not comment on formatting or style that an automatic formatter/linter would catch.
- Only comment on lines that were added or changed in the diff.
- Be specific and actionable. Every finding must include a concrete suggestion.
- Prefer few high-signal findings over many trivial ones. Zero findings is a valid outcome.
- NEVER raise anything covered by the "Team pushback" section. The team already decided.

Output format: respond with ONLY a JSON object (no markdown fences, no prose):
{
  "summary": "1-3 sentence overall assessment",
  "findings": [
    {
      "file": "path/from/diff",
      "line": <line number from the left gutter of the annotated diff, or null>,
      "severity": "blocker" | "major" | "minor" | "nit",
      "category": "bug" | "security" | "error-handling" | "performance" | "architecture" | \
"testing" | "readability" | "convention",
      "pattern_key": "short-kebab-case-id for the TYPE of issue, reused across reviews \
(e.g. missing-error-handling, sql-string-concat, unbounded-query)",
      "message": "what is wrong and why it matters",
      "suggestion": "how to fix it (code snippet welcome)"
    }
  ]
}
"""


def build_memory_context(rules, mistakes, pushback, standards_md: str | None) -> str:
    parts: list[str] = []

    if standards_md and standards_md.strip():
        parts.append("## Written team standards\n" + standards_md.strip())

    if rules:
        lines = [f"- [{r['category']}] {r['text']}" for r in rules]
        parts.append("## Learned team rules & architectural preferences\n" + "\n".join(lines))

    if mistakes:
        lines = [
            f"- {m['pattern_key']}: {m['description']} "
            f"(flagged {m['seen']}x, team accepted {m['accepted']}x)"
            for m in mistakes
        ]
        parts.append(
            "## Mistakes this team makes often (look for these extra carefully)\n"
            + "\n".join(lines))

    if pushback:
        lines = []
        for p in pushback:
            why = f" — reason: {p['reason']}" if p["reason"] else ""
            where = f" ({p['file']})" if p["file"] else ""
            lines.append(f"- {p['pattern_key']}{where}: {p['message'][:140]}{why}")
        parts.append(
            "## Team pushback: suggestions already REJECTED. Do not repeat them\n"
            + "\n".join(lines))

    return "\n\n".join(parts) if parts else "(No team memory yet. Use sound general judgment.)"


def build_review_prompt(memory_context: str, annotated_diff: str, pr_title: str | None = None,
                        pr_description: str | None = None) -> str:
    header = ""
    if pr_title:
        header += f"PR title: {pr_title}\n"
    if pr_description:
        header += f"PR description: {pr_description[:1500]}\n"
    return (
        f"# Team memory\n{memory_context}\n\n"
        f"# Change under review\n{header}\n"
        "The diff below has new-file line numbers in the left gutter. '+' = added, "
        "'-' = removed, blank = context.\n\n"
        f"{annotated_diff}\n\n"
        "Review this change now. Respond with the JSON object only."
    )


LEARN_SYSTEM_PROMPT = """\
You extract durable engineering rules from past code review comments so a review agent can \
apply them to future changes. Only extract rules that generalize (team conventions, \
architectural preferences, recurring mistakes). Ignore one-off or purely local remarks.

Respond with ONLY a JSON object:
{"rules": [{"text": "imperative rule, one sentence", "category": "convention|architecture|\
testing|security|error-handling|performance", "language": "python|javascript|typescript|java|\
go|rust|...|*"}]}
"""


def build_learn_prompt(comments: list[str]) -> str:
    joined = "\n".join(f"{i + 1}. {c.strip()}" for i, c in enumerate(comments) if c.strip())
    return f"Past review comments:\n\n{joined}\n\nExtract the rules. JSON only."
