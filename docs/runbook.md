# Operations Runbook

This is the day-to-day operating manual for the LinkedIn posting automation. When something breaks, when you want to change behavior, or when you're onboarding fresh — start here.

## How the system works (one paragraph)

A GitHub Actions workflow runs `linkedin_automation.py` every 15 minutes. Each run does three things in sequence: (1) fetches engagement stats for previously-posted rows and writes the Reach score back to the sheet, (2) processes any new Slack thread replies on in-flight drafts (approve / reject / regenerate), (3) once per day after 10 AM Sydney, generates a fresh draft from the next pending sheet row (or a fallback topic), runs it through plan-write-critique, and posts it to `#linkedin-posts` for review.

State that survives across runs lives in the hidden `_state` tab of the same Google Sheet — drafts in flight, idempotency keys, last-drafted date, member ID cache.

## Environments

There are two: **production** and **staging**.

| Environment | Branch | Workflow | Secrets prefix | Sheet | Slack channel |
|---|---|---|---|---|---|
| Production | `main` | `automation.yml` | (none) | `SHEET_ID` | `SLACK_CHANNEL_ID` |
| Staging | `staging` | `staging.yml` | `STAGING_` | `STAGING_SHEET_ID` | `STAGING_SLACK_CHANNEL_ID` |

The Anthropic key and LinkedIn token are shared across both. Don't share the Sheet or the Slack channel — staging needs to be safely separable from prod.

## Daily checklist (you, the human)

- Check `#linkedin-posts` once between 10 AM-noon Sydney for the daily draft.
- Reply in-thread with `approve`, `reject`, or `regenerate: <feedback>`.
- Refresh your Reach score column manually only if you want a snapshot — the engagement sync runs every 15 min and updates it automatically.

## Common operations

### Skip today's post

In the sheet's `_state` tab, find the `last_drafted_date` row and set its value (column B) to today's date in `YYYY-MM-DD` format. The next tick will see "already drafted today" and skip.

### Force-regenerate today's draft

In the `_state` tab, clear column B for `last_drafted_date`. Also remove today's entry from the `drafts` JSON list (column B for the `drafts` row). The next tick will draft fresh.

### Skip a specific row permanently

Set its `Status` to `skip` (or anything other than `pending` / blank / `drafted`). The picker only considers `pending` or empty status.

### Pause everything for a few days

GitHub repo → Settings → Actions → General → "Disable Actions". Re-enable when you want to resume. Existing in-flight drafts remain in state and will resume on the next run.

### Test a change without affecting prod

Push to the `staging` branch. The staging workflow runs against `STAGING_SHEET_ID` and `STAGING_SLACK_CHANNEL_ID`. By default `DRY_RUN=true` in staging — no actual LinkedIn posts. Watch staging for a few cycles, then merge `staging` → `main`.

### Trigger a manual run

Repo → Actions → linkedin-automation (production) → "Run workflow" → pick branch → Run.

## Failure scenarios and what to do

### Slack alerts you got "LinkedIn token rejected (401)"

The 60-day token expired. Regenerate following [secret_rotation.md](secret_rotation.md). Update the `LINKEDIN_TOKEN` secret. Next tick resumes; engagement sync and posting both work again.

### Slack alerts you with a Python traceback

Something crashed inside `main()`. Check the most recent failed run's logs in Actions. Common causes:
- Sheet API quota hit (rare; usually self-recovers)
- Anthropic API down (circuit breaker opens; recovers automatically)
- Sheet structurally changed (column you renamed; check the `[sheets]` log lines for "No column mapped for X")

### A draft posted twice to LinkedIn

Should not happen with idempotency keys, but if it does:
1. Manually delete the duplicate from LinkedIn
2. Find the offending entry in the `_state` tab's `drafts` value
3. Look at the `idempotency_key` and check the `idempotency` row in `_state` — there should only be one `linkedin_publish` mark for that key
4. If you find evidence of a duplicate publish for the same key, that's a real bug — file an issue and don't restart until it's fixed

### A draft never appears at 10 AM

Check in order:
1. Did GitHub Actions run? Repo → Actions tab. Look for runs at 10:00-10:15 Sydney.
2. Did the run succeed? If green, look at logs for "Daily draft already generated" or "Too early".
3. If it logged "Too early" but the time was past 10:00 Sydney — check `TZ` env in the workflow YAML.
4. If state says "already drafted" but you don't see a Slack message — check Slack app permissions; the bot may have been kicked.

### The bot stopped posting in `#linkedin-posts`

Was the bot kicked from the channel? In Slack: open `#linkedin-posts` → channel name → Members tab. If the bot is missing, `/invite @LinkedIn Poster`.

### Engagement sync is reporting errors

Check the `[engagement]` log lines. If LinkedIn returns 401 → token expired, see above. If 404s → posts were deleted from your profile (harmless, just noise). If 429s → rate limited, the retry handler will back off.

## Cost monitoring

- Anthropic API: spend dashboard at <https://console.anthropic.com/settings/billing>. Expected ~$0.025/post, so ~$0.75/month for daily posting.
- GitHub Actions: free tier covers this automation comfortably (about 30-40 minutes/month of compute).

## When to update prompts

Edit `prompts/about_me.md` / `prompts/voice_examples.md` / `prompts/topics.md` directly in the repo. Push to `main`. Changes apply on the next tick. No code change needed.

To change the **post format rules** (length, hashtags, style), edit `BASE_RULES` in `generator.py`. Run tests, push.

## When to update generation logic

If you want a different generation strategy (e.g. add a new step, change the model), the generation pipeline is in `generator.py`. The three steps (`_plan_post`, `_write_post_from_plan`, `_critique_post`) compose in `generate_post()`. Each is independently testable.

## Disabling without deleting

Repo → Settings → Actions → General → "Disable Actions". Stops the cron. All your code/state stays put. Re-enable any time.

## Full teardown

1. Disable Actions
2. Revoke the LinkedIn dev app at <https://www.linkedin.com/developers/apps>
3. Revoke the Slack app at <https://api.slack.com/apps> → your app → Basic Information → "Delete App"
4. Delete the Anthropic API key
5. Delete the Google Cloud service account
6. Delete the GitHub repo
