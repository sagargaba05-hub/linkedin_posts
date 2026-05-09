# Secret Rotation Runbook

When (not if) you need to rotate a credential, follow the relevant section. Always rotate by **adding the new secret first**, then **revoking the old one** — the opposite order causes downtime.

## LinkedIn access token (every 60 days, or on suspected compromise)

The script will Slack-alert you 5 days before expiry, and immediately on a 401. Either trigger means: rotate now.

1. Go to <https://www.linkedin.com/developers/apps> → your app → **Auth** tab.
2. Scroll to "OAuth 2.0 tools" → **Token Generator** (or equivalent — LinkedIn's UI moves around).
3. Select scopes: `w_member_social`, `openid`, `profile`. Authorize. Copy the new token (starts with `AQ...`).
4. In GitHub repo → Settings → Secrets and variables → Actions → click `LINKEDIN_TOKEN` → **Update**.
5. Paste the new token. Save.
6. Trigger a manual workflow run (Actions → automation (production) → Run workflow). Verify the run is green and `[linkedin]` log shows "Member ID resolved to ...".
7. The expiry-alert state in `_state` resets automatically once a successful auth call goes through with a fresh `first_seen` timestamp. (Cleared by `record_token_first_seen`.)

**If you forget for >60 days:** posts stop, engagement sync starts erroring. Slack alerts on every tick. No data loss — just downtime until you rotate.

## Slack bot token

Rotate when:
- A team member who installed the bot leaves
- The token might have leaked
- Slack tells you it has been rotated automatically

Steps:
1. <https://api.slack.com/apps> → your app → **Install App** → click **Reinstall to Workspace**.
2. Approve. Copy the new Bot User OAuth Token (`xoxb-...`).
3. GitHub → repo Secrets → update `SLACK_BOT_TOKEN`.
4. Re-invite the bot to `#linkedin-posts` if necessary (`/invite @bot-name`).
5. Manual workflow run to verify.

**Note:** reinstalling sometimes kicks the bot out of channels. Check membership after.

## Anthropic API key

Rotate when:
- The key might have leaked
- You want to scope a fresh key to a different budget

Steps:
1. <https://console.anthropic.com/> → API Keys → **Create Key**. Copy it.
2. GitHub → update `ANTHROPIC_API_KEY` to the new value.
3. Manual workflow run to verify generation works.
4. Once confirmed working, return to the Anthropic console and **delete the old key**.

## Google Cloud service account JSON

Rotate when:
- The key file might have leaked (e.g. accidentally committed)
- Quarterly rotation policy

Steps:
1. <https://console.cloud.google.com/> → IAM & Admin → Service Accounts → click your `linkedin-poster-sa` → **Keys** tab.
2. **Add key → Create new key → JSON**. A new JSON file downloads.
3. Open the new file. Copy the entire contents.
4. GitHub → update `GOOGLE_SERVICE_ACCOUNT_JSON` to the new contents.
5. Manual workflow run to verify sheet read/write works (`[sheets]` log shows "Mapped queue columns: ...").
6. Once confirmed, return to GCP Keys tab and **delete the old key**.
7. The `client_email` in the JSON should be unchanged — no need to re-share the sheet.

## SHEET_ID

Only rotate if you're moving to a new sheet entirely.

1. Create the new sheet, copy the column headers exactly from the old one.
2. Share it with the service account email as Editor.
3. Update `SHEET_ID` secret in GitHub.
4. **Important:** the new sheet has no `_state` tab yet, so the script treats this as a fresh start (no in-flight drafts, no member-ID cache, no idempotency history). If you have in-flight drafts, finish them on the old sheet before switching.

## SLACK_CHANNEL_ID

Easy. Get the new channel ID from its URL (`/C0XXXXXXXX`). Update the secret. Invite the bot to the new channel. Done.

## Emergency: a secret leaked (committed to a public repo, posted in chat, etc.)

For all six secrets, the recovery playbook is the same:

1. **Stop the bleeding.** Disable Actions (Repo → Settings → Actions → General → Disable). This freezes any automation that might use the leaked secret.
2. **Revoke the leaked secret at the source** (LinkedIn dev console, Anthropic console, etc.) — not just rotate; *revoke*.
3. **Generate a replacement.**
4. **Update the GitHub secret.**
5. **Re-enable Actions.**
6. **Audit usage** wherever possible (LinkedIn doesn't expose this; Anthropic does in the dashboard).
7. **Find the leak source and remove it.** If it was committed to git, follow [GitHub's remove-sensitive-data guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) — note that even after force-pushing, the secret remains accessible until you revoke it at the source.

## Setting up secrets for the first time (or for staging)

| Secret | Production | Staging | What it looks like |
|---|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✓ | `STAGING_GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON file contents, no wrapper quotes |
| `SHEET_ID` | ✓ | `STAGING_SHEET_ID` | The 44-char ID from the sheet URL |
| `SLACK_CHANNEL_ID` | ✓ | `STAGING_SLACK_CHANNEL_ID` | `C0XXXXXXXX` |
| `SLACK_BOT_TOKEN` | ✓ | (shared) | `xoxb-...` |
| `ANTHROPIC_API_KEY` | ✓ | (shared) | `sk-ant-...` |
| `LINKEDIN_TOKEN` | ✓ | (shared) | Long token starting `AQ...` |

For staging: create a separate sheet with the same column structure, share it with the same service account, and create a separate Slack channel (`#linkedin-drafts-staging`). Invite the bot to both.

## Audit log

When you rotate any secret, jot a one-liner in your password manager or a private note:

```
2026-05-04: rotated LINKEDIN_TOKEN (60-day expiry hit)
2026-07-03: rotated LINKEDIN_TOKEN (60-day expiry hit)
2026-09-01: rotated GOOGLE_SERVICE_ACCOUNT_JSON (quarterly)
```

This is the cheapest possible audit log. If a secret leak ever happens, this log tells you which versions were live during what window.
