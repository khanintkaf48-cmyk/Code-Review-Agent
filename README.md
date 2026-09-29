# Code Review Agent

A code review agent that **learns your team's coding standards, common mistakes and
architectural preferences over time**, and **remembers past review feedback so it never
repeats suggestions your team already rejected.**

> Code review is slow and inconsistent. An agent that knows your codebase's patterns and your
> team's conventions catches issues a generic linter never would.

## Try it in 60 seconds (no API key needed)

**Windows:** double-click `start-demo.bat`. **Mac/Linux:** `bash start-demo.sh`.
The first run creates a virtual environment and installs everything (needs Python 3.10+ and internet),
then opens the dashboard in your browser. Keep the window open; `Ctrl+C` stops it.

Manual way:

```bash
pip install -e .
reviewer demo                 # narrated terminal walkthrough of the learning loop
reviewer serve --demo --open  # dashboard at http://localhost:8080 with sample data
```

`--demo` swaps Claude for a scripted model that only knows the built-in sample diff
(click **Load a sample diff**). The learning logic (memory, suppression, feedback) is the real thing.
Demo data lives in `.reviewer/demo/`, is wiped on every start, and the **Reset demo** button in the
dashboard returns to the starting state between runs.

**What's included:** CLI · web dashboard · GitHub + GitLab (review, inline comments, webhooks,
learn from history) · Slack (notifications with Accept/Reject buttons, `/reviewer` slash command) ·
Docker · CI workflow.

## Real reviews (needs an API key)

```bash
# Windows                              # Mac / Linux
set ANTHROPIC_API_KEY=sk-ant-...       export ANTHROPIC_API_KEY=sk-ant-...
reviewer serve                         reviewer serve
```
Get a key at console.anthropic.com (paid). Paste a `git diff` output **or plain code** into
*Review a diff*. Plain code is reviewed as a new file.

## Troubleshooting

| Problem | Fix |
|---|---|
| `reviewer` not recognized | Activate the venv first: `.venv\Scripts\activate` (Windows) / `source .venv/bin/activate` |
| `pip` / `python` not recognized | Reinstall Python and tick **Add python.exe to PATH** |
| "Can't start on port 8080" | Another program uses it. Use `reviewer serve --demo --port 9000` |
| "ANTHROPIC_API_KEY is not set" | Set the key (see above) or use `--demo` |
| Old page after an update | Hard-refresh the browser: `Ctrl+Shift+R` |
| Demo says "only knows the built-in sample" | Expected: use *Load a sample diff*, or run with a real key |
| Different memory in different folders | Memory is stored in `.reviewer/` of the folder you run from; set `REVIEWER_HOME` to share one |

## How it works

```
 diff ──► parse + number lines ──► [team memory injected] ──► Claude ──► findings
                                          ▲                                 │
                                          │                        filter out already-rejected
                                          │                                 │
   feedback (accepted / rejected + why) ──┴──────── SQLite memory ◄── record findings
```

Memory (`.reviewer/memory.db`, SQLite) holds:

| Store      | What it remembers                                             | Effect on next review                              |
|------------|---------------------------------------------------------------|----------------------------------------------------|
| `rules`    | Standards, architecture preferences, exceptions               | Injected into the prompt (filtered by language)    |
| `findings` | Every suggestion + the team's verdict and reason              | Rejected ones are hidden and listed as "pushback"  |
| `patterns` | Per issue-type counters: seen / accepted / rejected           | Often-accepted patterns get extra attention; patterns rejected repeatedly are silenced team-wide |

Three ways the agent learns:
1. **`reviewer teach`** / `.reviewer/standards.md` – you state the rules.
2. **`reviewer learn`** – it reads old PR review comments and extracts durable rules.
3. **`reviewer feedback`** – accept/reject findings. A rejection *with a reason* becomes a permanent
   "do not flag" rule.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=ghp_...        # only for GitHub features
```

Optional: `REVIEWER_MODEL` (default `claude-sonnet-5-5`), `REVIEWER_HOME` (default `.reviewer`).

## Usage

```bash
# Review local changes
reviewer review                       # uncommitted changes vs HEAD
reviewer review --base main           # current branch vs main
reviewer review --staged
git diff main | reviewer review --diff-file -

# Review a GitHub PR (and post inline comments)
reviewer review --repo acme/api --pr 123 --post

# Teach it
reviewer teach "Handlers must not import from repositories" --category architecture --language python
reviewer learn --repo acme/api --prs 40         # mine past review comments
reviewer learn --file old_comments.json         # or a JSON list / text file

# Give feedback (this is what makes it smarter)
reviewer feedback 12 accepted
reviewer feedback 13 rejected --reason "we validate input in the API gateway"

# Inspect
reviewer rules
reviewer rules --delete 4
reviewer stats
```

Every finding is printed with an id (`#12`) — use it with `reviewer feedback`.

### In CI
See `.github/workflows/review.yml`. The memory DB is cached between runs so learning
persists. For a team, better options are committing `standards.md` (already supported) and/or
storing `memory.db` on shared storage.

## Dashboard

```bash
reviewer serve                      # http://localhost:8080
reviewer serve --host 0.0.0.0 --port 8080   # expose it -> set REVIEWER_DASHBOARD_TOKEN first!
```

Overview (what it learned), Findings (accept / reject with a reason), Team rules (add / delete),
and *Review a diff* (paste a diff, judge results inline). The same server exposes the JSON API and
the webhooks below. It uses only the Python standard library.

## GitHub

```bash
export GITHUB_TOKEN=...             # repo scope
reviewer review --repo acme/api --pr 123 --post --slack
reviewer learn --repo acme/api --prs 40
```
**Auto-review every PR:** run `reviewer serve` somewhere reachable, then add a repo webhook →
`https://<host>/webhook/github`, content type `application/json`, event *Pull requests*, and set the
same secret as `GITHUB_WEBHOOK_SECRET`. (Or use the ready-made `.github/workflows/review.yml`.)

## GitLab

```bash
export GITLAB_TOKEN=...             # api scope; GITLAB_URL=https://gitlab.example.com if self-hosted
reviewer review --gitlab-project group/name --mr 42 --post --slack
reviewer learn --gitlab-project group/name --prs 40
```
**Auto-review every MR:** project → Settings → Webhooks → URL `https://<host>/webhook/gitlab`,
secret token = `GITLAB_WEBHOOK_TOKEN`, trigger *Merge request events*. Findings are posted as inline
discussions; anything that can't be placed on a line goes into one summary note.

## Slack

1. Create a Slack app. Enable **Incoming Webhooks** → `SLACK_WEBHOOK_URL`.
2. **Interactivity** → request URL `https://<host>/slack/interactions` (makes the Accept/Reject
   buttons work).
3. **Slash Commands** → `/reviewer`, request URL `https://<host>/slack/commands`.
4. Copy the app's *Signing Secret* → `SLACK_SIGNING_SECRET`. Every request is signature-verified
   and replay-protected.

Then `reviewer review --slack ...` (or the webhooks) posts each review with buttons, and in Slack:
`/reviewer stats`, `/reviewer rules`, `/reviewer teach <rule>`, `/reviewer feedback <id> rejected <why>`.

## Docker

```bash
cp .env.example .env    # fill in the values
docker compose up -d    # dashboard + webhooks on :8080, memory persisted in a volume
```

## Security notes

- Webhooks/Slack endpoints refuse requests unless their secret env var is set and the signature or
  token matches (constant-time comparison).
- The dashboard binds to `127.0.0.1` by default. If you expose it, set `REVIEWER_DASHBOARD_TOKEN`
  (protects `/api/*`), and put it behind HTTPS (e.g. a reverse proxy).
- The diff you review is sent to the Anthropic API. Don't point this at code you can't share.

## Project layout

```
src/reviewer/
  cli.py                 commands
  agent.py               review loop: memory -> Claude -> filter -> record; learn-from-history
  memory.py              SQLite store, suppression + learning logic
  prompts.py             system prompt + memory-context builder
  diff_utils.py          diff parsing, line numbering, line snapping
  server.py              dashboard + JSON API + webhooks (stdlib HTTP server)
  web/index.html         dashboard UI (single file, works offline)
  jobs.py                review PR/MR -> post -> notify (shared by CLI and webhooks)
  github_integration.py  fetch PRs / comments, post reviews
  gitlab_integration.py  fetch MRs / comments, post inline discussions
  slack_integration.py   notifications, buttons, slash commands, signature check
  demo.py                offline scripted model + narrated walkthrough + sample data
start-demo.bat / .sh     one-click setup + launch of the demo dashboard
tests/                   50+ tests (fake Claude client, no API key needed)
```

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Design notes / extending

- Findings pointing at files or lines not in the diff are dropped/snapped, so hallucinated
  locations never reach your PR.
- **Safety net:** findings with severity `blocker` are never hidden, even if the team rejected
  them before. They come back marked "rejected before, shown again because it is a blocker".
  Configure with `Config.never_suppress_severities` (default `("blocker",)`).
- Suppression rules (see `Memory.is_suppressed`): exact repeat of a rejected suggestion; same issue
  type rejected on the same file; issue type rejected >= 2 times and more often than accepted.
  Tune with `Config.suppress_after_rejects`.
- Ideas: embeddings-based retrieval of relevant rules for very large rule sets; auto-detect "fixed"
  findings by re-diffing; per-directory rules; Bitbucket / Teams integrations.
