"""Helpers for getting and understanding unified diffs."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ParsedDiff:
    """Result of parsing a unified diff."""
    # file path -> set of line numbers (in the new file) that were added
    added_lines: dict[str, set[int]] = field(default_factory=dict)
    # file path -> set of all visible new-file line numbers (added + context)
    visible_lines: dict[str, set[int]] = field(default_factory=dict)
    # diff text with new-file line numbers prefixed, for the LLM
    annotated: str = ""

    @property
    def files(self) -> list[str]:
        return list(self.added_lines)


def parse_diff(diff: str) -> ParsedDiff:
    result = ParsedDiff()
    out: list[str] = []
    current: str | None = None
    new_ln = 0
    in_hunk = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            in_hunk = False
            current = None
            out.append(raw)
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path == "/dev/null":
                current = None
            else:
                current = path[2:] if path.startswith("b/") else path
                result.added_lines.setdefault(current, set())
                result.visible_lines.setdefault(current, set())
            out.append(f"=== FILE: {current or '(deleted)'} ===")
            continue
        if raw.startswith("--- ") and not in_hunk:
            continue
        m = HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(1))
            in_hunk = True
            out.append(raw)
            continue
        if not in_hunk or current is None:
            if raw.startswith(("index ", "new file", "deleted file", "similarity", "rename ",
                               "old mode", "new mode", "Binary files")):
                continue
            continue
        if raw.startswith("+"):
            result.added_lines[current].add(new_ln)
            result.visible_lines[current].add(new_ln)
            out.append(f"{new_ln:>5} + {raw[1:]}")
            new_ln += 1
        elif raw.startswith("-"):
            out.append(f"      - {raw[1:]}")
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            result.visible_lines[current].add(new_ln)
            out.append(f"{new_ln:>5}   {raw[1:] if raw.startswith(' ') else raw}")
            new_ln += 1

    result.annotated = "\n".join(out)
    return result


def snap_line(parsed: ParsedDiff, file: str, line: int | None, tolerance: int = 3) -> int | None:
    """Return a valid changed line close to ``line`` or None if there isn't one."""
    added = parsed.added_lines.get(file)
    if not added or line is None:
        return None
    if line in added:
        return line
    nearest = min(added, key=lambda x: abs(x - line))
    return nearest if abs(nearest - line) <= tolerance else None


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rsplit("\n", 1)[0]
    return cut + "\n... [diff truncated] ...", True


def git_diff(base: str | None = None, staged: bool = False, cwd: str | None = None) -> str:
    """Get a diff from the local git repo."""
    if base:
        cmd = ["git", "diff", f"{base}...HEAD"]
    elif staged:
        cmd = ["git", "diff", "--cached"]
    else:
        cmd = ["git", "diff", "HEAD"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git failed: {proc.stderr.strip()}")
    return proc.stdout


def looks_like_diff(text: str) -> bool:
    """True if the text is a unified diff (git style or plain `diff -u`)."""
    return bool(re.search(r"^diff --git ", text, re.M)
                or re.search(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", text, re.M))


def new_file_diff(path: str, text: str) -> str:
    """Express a whole file as a diff that adds it (used for pasted plain code)."""
    lines = text.splitlines()
    body = "\n".join("+" + line for line in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}\n")


def guess_filename(text: str) -> str:
    if re.search(r"^\s*(def |class |import |from \w+ import |print\()", text, re.M):
        return "pasted_code.py"
    if re.search(r"\b(function |const |let |console\.log)", text):
        return "pasted_code.js"
    return "pasted_code.txt"
