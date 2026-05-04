# LinkedIn Posting Automation

Daily LinkedIn post generation, Slack-based review, and auto-publishing — all running on GitHub Actions, no local machine required.

## How it works

1. **Every 15 minutes**, GitHub Actions runs `linkedin_automation.py`.
2. **Once per day** (after the configured hour, default 10 AM IST), the script picks the next pending row from the Google Sheet, generates a draft via Claude, and posts it to `#linkedin-posts` for review.
3. **You reply** in the Slack thread with `approve`, `reject`, or `regenerate: <feedback>`.
4. The next 15-minute tick processes your reply: on `approve`, the script publishes to LinkedIn and writes the live post URL back to the sheet.

State (which rows are mid-flight, the cached LinkedIn member ID, last-drafted-date) lives in a hidden `_state` tab inside the same sheet, so this repo never holds runtime state.

## One-time setup

### 1. Google Cloud service account (for Sheets read/write)

1. Go to <https://console.cloud.google.com/> and create a new project (e.g. `linkedin-poster`).
2. In the sidebar: **APIs & Services → Library**. Search for "Google Sheets API", click **Enable**. Repeat for "Google Drive API".
3. **APIs & Services → Credentials → Create credentials → Service account.**
   - Name: `linkedin-poster-sa`
   - Skip the optional steps; click **Done**.
4. Click the new service account, go to **Keys → Add key → Create new key → JSON**. A file downloads. **Open the file in a text editor; you'll paste the entire contents into a GitHub secret in step 5.**
5. Open the JSON file and copy the `client_email` value (looks like `linkedin-poster-sa@your-project.iam.gserviceaccount.com`). In the LinkedIn queue Google Sheet, click **Share** and add this email as **Editor**. (This is how the script gets write access — without this step, every Sheets call returns 403.)

### 2. Slack app (for posting drafts and reading approvals)

1. Go to <https://api.slack.com/apps> → **Create New App → From scratch**. Name: `LinkedIn Poster`. Workspace: yours.
2. **OAuth & Permissions → Scopes → Bot Token Scopes**, add:
   - `chat:write` (post messages)
   - `channels:history` (read replies in public channels)
   - `channels:read` (look up channel info)
   - `groups:history` and `groups:read` (only if `#linkedin-posts` is private)
3. Scroll up, click **Install to Workspace**. Approve. Copy the **Bot User OAuth Token** (starts with `xoxb-`). This is your `SLACK_BOT_TOKEN`.
4. In Slack, type `/invite @LinkedIn Poster` in `#linkedin-posts` to add the bot to the channel.
5. Get the channel ID: open `#linkedin-posts` in a web browser; the URL ends with `/C0XXXXXXXX` — that suffix is your `SLACK_CHANNEL_ID`.

### 3. Anthropic API key (for draft generation)

1. Go to <https://console.anthropic.com/> → sign in.
2. **API Keys → Create Key.** Copy it (starts with `sk-ant-`). This is your `ANTHROPIC_API_KEY`.
3. **Plans & Billing → Add credits.** $5 is plenty for a year of daily posts.

### 4. LinkedIn access token

You already have this. If it expires (every ~60 days), regenerate via the LinkedIn developer app's **Auth → Token Generator** with scopes `w_member_social openid profile`, then update the `LINKEDIN_TOKEN` secret in this repo.

### 5. GitHub repo secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret.** Add these seven:

| Name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The entire contents of the service account JSON file from step 1.4 |
| `SHEET_ID` | `115PyzTascK_6pIuG1dfajC4cIAEsIwezkWcn_RcVKEQ` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `SLACK_CHANNEL_ID` | `C0XXXXXXXX` (the ID, not the name) |
| `LINKEDIN_TOKEN` | Your LinkedIn access token |

## First run / testing

1. Push this repo to GitHub: `git add . && git commit -m "initial" && git push`.
2. In your repo on GitHub: **Actions → linkedin-automation → Run workflow.** Set "Skip actual LinkedIn POST" to `true` for the first run. This does everything (reads sheet, generates draft, posts to Slack, polls thread) **except** the actual LinkedIn POST.
3. Verify in Slack: a draft should appear in `#linkedin-posts`.
4. Reply in the thread with `approve`. Wait ~15 min for the next scheduled run, or manually trigger again.
5. With dry-run on, the script will *say* it posted (with a placeholder URL) without actually hitting LinkedIn. Once you're happy, repeat without dry-run.

## Daily usage

- Add new rows to the Google Sheet whenever you have ideas. Set `Status = pending`, fill in `Topic` and `Angle` at minimum (the more you fill in, the better the draft).
- Each morning, a draft lands in `#linkedin-posts`. Reply `approve` / `reject` / `regenerate: <feedback>`.
- After publishing, the script writes the live post URL back to your sheet's `post URL` column. Fill in `Reach score` later when you want to track engagement.

## Troubleshooting

**No draft appearing.** Check the Actions tab logs. Common causes: a secret is missing/typo'd, the service account hasn't been shared on the sheet, the bot wasn't invited to the channel.

**Draft appears but `approve` doesn't trigger a post.** The script needs the `channels:history` scope to read your reply. Re-check the Slack app's OAuth scopes; if you added scopes after installing, you must reinstall the app to your workspace.

**LinkedIn API returns 401.** Token expired. Regenerate (see step 4) and update the `LINKEDIN_TOKEN` secret.

**Want to skip a row?** Set its `Status` to anything other than `pending` (e.g. `skip`).

**Want to force-regenerate today's draft from scratch?** Open the `_state` tab in the sheet, find the `last_drafted_date` row, clear column B. Next run will draft again. Also clear or delete that day's entry from `drafts` if you want a clean state.

## Cleanup if you ever want to disable

1. Repo → **Settings → Actions → General → Disable Actions.** Stops the cron immediately.
2. Optional: delete the GitHub repo, revoke the LinkedIn dev app, revoke the Slack app, delete the service account, delete the Anthropic API key.
