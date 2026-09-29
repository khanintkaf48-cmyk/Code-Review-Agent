"""The review agent: loads memory -> asks Claude -> filters -> records."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field

from .config import Config
from .diff_utils import (guess_filename, looks_like_diff, new_file_diff, parse_diff,
                         snap_line, truncate)
from .memory import Memory, language_of, slugify
from .prompts import (LEARN_SYSTEM_PROMPT, SYSTEM_PROMPT, build_learn_prompt,
                      build_memory_context, build_review_prompt)

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}


@dataclass
class ReviewResult:
    review_id: str
    summary: str
    findings: list[dict] = field(default_factory=list)
    suppressed: list[dict] = field(default_factory=list)
    truncated: bool = False


def extract_json(text: str) -> dict:
    """Parse the model's JSON reply, tolerating code fences / stray prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("Model did not return valid JSON") from None


NO_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set, so I can't call Claude.\n"
    "  1) Get a key at console.anthropic.com\n"
    "  2) Windows:    set ANTHROPIC_API_KEY=sk-ant-...\n"
    "     Mac/Linux:  export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  3) Run again.  (No key? Try the offline demo: reviewer serve --demo)")


def _make_client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(NO_KEY_MESSAGE)
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Install dependencies first: pip install -r requirements.txt") from e
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


class ReviewAgent:
    def __init__(self, config: Config | None = None, memory: Memory | None = None, client=None):
        self.config = config or Config()
        self.memory = memory or Memory(self.config.db_path)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = _make_client()
        return self._client

    # ------------------------------------------------------------------ LLM
    def _ask(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def _ask_json(self, system: str, user: str, attempts: int = 2) -> dict:
        """Ask for JSON; if the model's answer is unreadable, try once more."""
        for attempt in range(attempts):
            try:
                data = extract_json(self._ask(system, user))
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
        raise ValueError("Claude returned an unreadable answer twice. Please try again.")

    # --------------------------------------------------------------- review
    def _memory_context(self, files: list[str]) -> str:
        cfg = self.config
        langs = {language_of(f) for f in files}
        rules = self.memory.list_rules(languages=langs, limit=cfg.max_rules_in_prompt)
        mistakes = self.memory.frequent_mistakes(cfg.max_mistakes_in_prompt)
        pushback = self.memory.recent_pushback(cfg.max_pushback_in_prompt)
        standards = None
        if cfg.standards_path.exists():
            standards = cfg.standards_path.read_text(encoding="utf-8")
        return build_memory_context(rules, mistakes, pushback, standards)

    def review(self, diff: str, pr_title: str | None = None,
               pr_description: str | None = None) -> ReviewResult:
        review_id = uuid.uuid4().hex[:8]
        if not diff.strip():
            return ReviewResult(review_id, "No changes to review.")

        if not looks_like_diff(diff):
            # Plain code was pasted instead of a diff: review it as a brand-new file.
            diff = new_file_diff(guess_filename(diff), diff)

        parsed = parse_diff(diff)
        if not parsed.files:
            return ReviewResult(review_id, "Couldn't find any changed files in that input. "
                                           "Paste the output of `git diff`, or plain code.")
        annotated, was_truncated = truncate(parsed.annotated, self.config.max_diff_chars)
        prompt = build_review_prompt(self._memory_context(parsed.files), annotated,
                                     pr_title, pr_description)

        data = self._ask_json(SYSTEM_PROMPT, prompt)
        kept, suppressed = self._filter(data.get("findings", []), parsed)

        kept.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["file"], f["line"] or 0))
        self.memory.record_review(review_id, kept)
        return ReviewResult(review_id, data.get("summary", ""), kept, suppressed, was_truncated)

    def _filter(self, raw_findings: list[dict], parsed) -> tuple[list[dict], list[dict]]:
        kept, suppressed, seen = [], [], set()
        for f in raw_findings:
            file, message = f.get("file"), (f.get("message") or "").strip()
            if not file or not message or file not in parsed.added_lines:
                continue  # hallucinated file or empty comment
            f["severity"] = f.get("severity") if f.get("severity") in SEVERITY_ORDER else "minor"
            f["pattern_key"] = slugify(f.get("pattern_key") or f.get("category") or message[:40])
            f["line"] = snap_line(parsed, file, f.get("line"))
            f["message"] = message

            key = (f["pattern_key"], file, f["line"])
            if key in seen:
                continue  # model repeated itself
            seen.add(key)

            rejected_before = self.memory.is_suppressed(
                f["pattern_key"], file, message, self.config.suppress_after_rejects)
            if not rejected_before:
                kept.append(f)
            elif f["severity"] in self.config.never_suppress_severities:
                # Safety net: serious issues resurface, flagged so the reviewer knows why.
                f["previously_rejected"] = True
                kept.append(f)
            else:
                suppressed.append(f)
        return kept, suppressed

    # ---------------------------------------------------------------- learn
    def learn_from_comments(self, comments: list[str], source: str = "history") -> list[dict]:
        """Turn old review comments into stored team rules. Returns rules that were new."""
        comments = [c for c in comments if c and c.strip()]
        if not comments:
            return []
        added: list[dict] = []
        # Batch to keep prompts small.
        for i in range(0, len(comments), 40):
            data = self._ask_json(LEARN_SYSTEM_PROMPT, build_learn_prompt(comments[i:i + 40]))
            for r in data.get("rules", []):
                rid = self.memory.add_rule(r.get("text", ""), r.get("category", "general"),
                                           r.get("language", "*"), source=source)
                if rid:
                    added.append(r)
        return added
