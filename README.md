# LinkedIn Posting Automation (v2)

Daily LinkedIn post generation, Slack-based review, automatic publishing, and an engagement feedback loop — running on GitHub Actions, no local machine required.

## How it works

Every 15 minutes, a GitHub Actions workflow runs `linkedin_automation.py`. Each tick does three phases:

1. **Engagement sync.** For every previously-posted row, fetch likes/comments from LinkedIn and write a freshly-computed `Reach score` back to the sheet.
2. **Process Slack replies.** For every in-flight draft, look at its Slack thread and act on `approve` / `reject` / `regenerate: <feedback>`. Approve publishes to LinkedIn (with idempotency protection — never double-posts).
3. **Daily draft.** Once per day after 10 AM Sydney, pick the next pending sheet row (or fall back to a topic from `prompts/topics.md`), inject your top-performing past posts as few-shot examples, run a plan→write→critique pipeline, and post the draft to `#linkedin-posts`.

State (drafts in flight, idempotency keys, last-drafted date, member ID) lives in a hidden `_state` tab inside the same Google Sheet, so the repo holds zero runtime state.

## Architecture

```
linkedin_posts/
├── linkedin_automation.py    # Entry point — wires phases together
├── config.py                 # Env vars, constants, logger factory
├── reliability.py            # Retries (tenacity), circuit breakers, idempotency
├── observability.py          # Slack error alerts, token expiry monitoring
├── sheets.py                 # Google Sheets adapter (queue + _state tab)
├── slack_helpers.py          # Slack adapter (post drafts, read replies)
├── linkedin_api.py           # LinkedIn adapter (publish + fetch stats)
├── generator.py              # plan→write→critique generation pipeline
├── engagement.py             # Stats sync + top-posts feedback loop
├── pipeline.py               # The three phases
├── prompts/                  # User-editable templates
│   ├── about_me.md
│   ├── voice_examples.md
│   └── topics.md
├── tests/                    # pytest unit tests
│   ├── conftest.py
│   ├── test_selection.py
│   ├── test_parsing.py
│   └── test_idempotency.py
├── docs/
│   ├── runbook.md            # Day-to-day operations
│   └── secret_rotation.md    # Secret rotation procedures
├── .github/workflows/
│   ├── automation.yml        # Production cron (main branch)
│   ├── staging.yml           # Staging cron (staging branch)
│   └── ci.yml                # Ruff + pytest on every PR
├── pyproject.toml            # Ruff + pytest config
├── .pre-commit-config.yaml   # Local pre-commit hooks
├── requirements.txt          # Production deps
├── requirements-dev.txt      # Dev deps (pytest, ruff)
└── .gitignore
```

Tagged loggers (`[main]`, `[config]`, `[sheets]`, `[slack]`, `[linkedin]`, `[gen]`, `[pipeline]`, `[reliability]`, `[observability]`, `[engagement]`) make GitHub Actions logs easy to grep.

## Reliability features

- **Retries with exponential backoff** on every external API call (Sheets, Slack, LinkedIn, Anthropic). Transient 5xx/429 errors recover automatically.
- **Circuit breakers** per service — if a service is genuinely down, stop hammering it for 5 minutes.
- **Idempotency keys** on every draft. The LinkedIn POST and sheet status writes are guarded by a `(key, op)` registry stored in the `_state` tab; impossible to double-post even if GitHub Actions misfires.
- **Abandoned-draft GC** — drafts with no reply after 36 hours are auto-marked `abandoned` so they stop being polled.
- **Error alerting** — any unhandled exception is posted to your Slack channel as a critical alert with the full traceback.
- **Token expiry monitoring** — LinkedIn token age is tracked from first-seen; warning at 5 days remaining, critical alert at expiry.

## The engagement feedback loop

After every approved post, the system automatically:

1. Records the post URN (used to fetch stats later).
2. On every subsequent tick, calls LinkedIn's socialActions endpoint to get fresh like + comment counts.
3. Computes `reach_score = likes + 2 * comments` and writes it back to your sheet's `Reach score` column.
4. When generating a new draft, loads your top 3 highest-scoring past posts and injects them as few-shot examples in the system prompt.

The model effectively learns over time what your audience engages with, without any manual tuning.

## Sheet columns

| Column | Read by | Written by |
|---|---|---|
| `SNo.` | picker | (you, optional) |
| `Date` | picker (sort order) | (you) |
| `Topic` | generator | (you) |
| `Angle` | generator (most important field for non-generic output) | (you) |
| `Key points` | generator | (you) |
| `Voice` | generator (`thoughtful` / `conversational` / `punchy`) | (you) |
| `Hook style` | generator (`question` / `story` / `contrarian` / `stat`) | (you) |
| `Link` | generator | (you) |
| `CTA` | generator | (you) |
| `Status` | picker | script (`drafted` / `posted` / `rejected`) |
| `post URL` | engagement-sync | script |
| `Reach score` | feedback-loop | script (auto-updated every tick) |
| `Notes` | (audit log only) | script (timestamped audit trail) |
| `Generated by (Sagar/Cowork)` | (display only) | script (`Sagar` for your rows, `Cowork` for fallbacks) |

## Setup, troubleshooting, secret rotation

See [docs/runbook.md](docs/runbook.md) for day-to-day operations and [docs/secret_rotation.md](docs/secret_rotation.md) for credential rotation procedures.

## Local development

```sh
git clone https://github.com/sagargaba05-hub/linkedin_posts.git
cd linkedin_posts
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pre-commit install           # Installs git hooks for ruff/format
pytest -v                    # Runs the test suite
ruff check .                 # Lints the codebase
ruff format .                # Auto-formats
```

## Cost expectations

- Anthropic API: ~$0.025/post = ~$0.75/month for daily posting (with prompt caching + Haiku critic).
- GitHub Actions: free tier (~30-40 min/month of compute).
- Google Sheets API: free tier (no usage charges at this scale).
- LinkedIn API: free.

## Disabling temporarily / fully

Repo Settings → Actions → General → "Disable Actions" stops the cron immediately. Re-enable any time. For full teardown see the bottom of [docs/runbook.md](docs/runbook.md).
