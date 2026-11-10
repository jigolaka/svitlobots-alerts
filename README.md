# Svitlobot Night Alerts

Scrapes the public Telegram previews for
[Shevchenkove](https://t.me/s/svitlobot_ShevchenkoveBL) and
[City Lake](https://t.me/s/svitlobot_juvileinyi), detects actual power-status
posts, and emails power-outage alerts through Gmail. No Telegram bot token,
Telegram account, browser automation, or paid service is required.

## Default Schedule

The timezone is assumed to be **Europe/Kyiv**. The workflow schedules short
checks at **00:00, 00:15, ... 05:45**, every night. It does not keep a runner
alive between checks. GitHub's timezone-aware cron handles daylight saving
changes; the Python guard also checks local time and skips late runs after 06:00.

GitHub Actions is not an exact-time or real-time scheduler. Runs can be delayed
or dropped, especially at the top of an hour. Detection is normally within
15 minutes of a post becoming visible, plus Telegram publication and GitHub
delays. These channels sometimes publish outage posts several minutes after
the event time printed in the message. This is not a safety-critical alarm.

To change the timezone, edit **both** `schedule[].timezone` and
`jobs.check.env.LOCAL_TIMEZONE` in [.github/workflows/nightly.yml](.github/workflows/nightly.yml).
To change hours, edit the cron hour range and `NIGHT_START_HOUR` /
`NIGHT_END_HOUR` together. The end hour is exclusive. For example, 00:00-04:00
uses `*/15 0-3 * * *`, start `0`, end `4`. Start `22`, end `6` supports an
overnight Python window; its matching cron hours would be `22-23,0-5`.

## GitHub Setup

1. Create a GitHub repository and push these project files to its default
   branch. Do not upload `.venv`, local state, or credentials. Enable Actions
   if prompted. Scheduled workflows only run from the default branch.
2. Enable Google 2-Step Verification for the sender Gmail account, then create
   an [App Password](https://support.google.com/accounts/answer/185833).
   Use this app password, **not your regular Gmail password**. Some managed
   accounts and security configurations do not allow app passwords.
3. Open **Settings > Secrets and variables > Actions > Secrets** and add:

| Repository Secret    | Value                                                 |
| -------------------- | ----------------------------------------------------- |
| `GMAIL_USERNAME`     | Sender's full Gmail address                           |
| `GMAIL_APP_PASSWORD` | Sender's Google App Password (without display spaces) |
| `ALERT_TO`           | Your destination Gmail address                        |

4. Allow the workflow's `contents: write` permission. Organization policies
   and branch rules must allow creation/writes to `svitlobot-state`. The
   workflow uses the automatic, short-lived `GITHUB_TOKEN`; **do not create a
   personal access token or a secret named GITHUB_TOKEN**.
5. In **Actions > Nightly Power Outages and Restoration Alerts > Run workflow**,
  select `dry-run` to verify live scraping without secrets, email, or state
  writes. Select `test-email` to verify Gmail delivery at any time. Select
  `test-restoration` to send clearly marked simulated restoration emails for
  both default channels; it does not fetch Telegram or change tracking state.
  Check spam if needed. Selecting `check` performs a normal check only during
  the night window.
6. Leave the schedule enabled. Optionally add repository **Variable**
   `NOTIFY_RESTORED=true` to receive recovery emails as well.

Never paste credentials into issues, logs, commits, or chat. Secrets are
passed only to the monitoring step, not to the test workflow. SMTP uses TLS
with certificate verification; the program does not log credentials or email
addresses. Test-email and test-restoration modes do not fetch Telegram or
change tracking state.

## Alert And State Rules

- Each channel is independent. A problem checking one does not prevent
  attempting the other; the workflow still fails so the error is visible.
- At the first successful check of each night, only the latest recognized
  status is used. If it is off, send an alert even if the outage started
  before midnight. If it is on, record the baseline without an email. This
  avoids replaying daytime outages. An outage continuing into another night
  produces one reminder on that night's first check.
- Later checks process all visible, unseen status posts in message-ID order.
  A brief outage followed by a recovery between checks is still reported.
  Email describes a status event, not a guarantee of the current status.
- Outages are always emailed. Restorations are tracked but only emailed when
  `NOTIFY_RESTORED=true`; an initial on-status baseline is never emailed.
- Schedule images, forecasts, and unrelated posts are ignored. Both Ukrainian
  status phrases and their corresponding red/green indicators must match.
- State contains only channel names, message IDs, and night dates. In Actions,
  it lives in `state.json` on the automatically created **svitlobot-state**
  branch, not in an expiring Actions cache or artifact. The branch initially
  points at the current code commit; it is not the deployment/default branch.
  No credential is stored there. The branch is public if your repo is public.
- Checkpoints advance only after required email delivery succeeds. Failed
  sends are attempted again on a later check. A crash or failed state write
  after SMTP accepts an email can still cause a duplicate. SMTP and GitHub
  cannot provide an atomic exactly-once transaction. Do not run multiple
  independent copies against the same state; workflow concurrency serializes
  this workflow's scheduled and manual runs.
- Only Telegram's latest preview page is fetched. If a checkpoint disappears
  from that page, a warning is logged and visible events are processed; older
  missing events cannot be recovered. A blocked or empty status page fails
  rather than being interpreted as power on. No readings means no reliable
  indication of actual power; stale posts can describe stale status.
- Delayed runs outside the window are skipped. There is no final 06:00 check,
  so events after the last successful check are not guaranteed to be reported.
  Monitoring is intentionally inactive during daytime.

Do not delete or force-push the state branch unless you want to reset tracking.
Protect the default branch normally, but ensure state-branch rules allow the
workflow to update it. Repository rules requiring signed commits may block
the Contents API writes used here.

## Free-Tier Budget

Standard GitHub-hosted runners in public repositories are free under GitHub's
current terms. GitHub Free private repositories normally include **2,000
Actions minutes/month**, shared with other workflows. Confirm your account's
[current allowances](https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions).

This schedule makes 24 short Linux jobs/night: 744 jobs in a 31-day month.
At one billed minute per job that is about **744 minutes/month**; at two it
is **1,488**. Actual rounded runner time, manual runs, retries, tests, and
other repositories count too. Dependency caching reduces setup overhead.
This is a budget-conscious design, not a guarantee of free usage. Check
usage and configure billing limits so paid overages are not permitted.

Keeping a job running for six hours every night would consume about 11,160
minutes/month. A five-minute polling cron would already create 2,232 jobs
in a 31-day month, before setup or retries, so neither is the private-free
default. Public repos can use a faster schedule, but GitHub cron's minimum
interval is five minutes and delays still apply.

Public-repo scheduled workflows are automatically disabled after 60 days
without repository activity. Check the Actions tab periodically and re-enable
if necessary. State-branch updates alone should not be relied on as a keepalive.

## Local Development (Windows PowerShell)

A local `.venv` was created for this workspace. On a fresh checkout, use
Python 3.14 (3.11+ is sufficient for the source):

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe monitor.py --dry-run
```

The dry run fetches both channels without sending anything or modifying
state, even outside the night window. It needs no credentials.

For a local email test, set `GMAIL_USERNAME` and `ALERT_TO` in your process
environment and enter the app password in a secure terminal prompt:

```powershell
$env:GMAIL_USERNAME = Read-Host 'Sender Gmail address'
$env:ALERT_TO = Read-Host 'Destination Gmail address'
$appPassword = Read-Host 'Google App Password' -AsSecureString
$env:GMAIL_APP_PASSWORD = [System.Net.NetworkCredential]::new('', $appPassword).Password
try {
    .\.venv\Scripts\python.exe monitor.py --test-email
} finally {
    Remove-Item Env:GMAIL_APP_PASSWORD -ErrorAction SilentlyContinue
    Remove-Variable appPassword -ErrorAction SilentlyContinue
}
```

To make a normal local night check, supply those same three environment
variables and run `python monitor.py` using the venv interpreter. Local state
defaults to `.state/state.json`, which is ignored by git. Local checks do not
run repeatedly on their own; Actions supplies the recurring schedule.
[.env.example](.env.example) documents settings but is **not automatically
loaded**. Configure environment variables directly; no dotenv dependency
is required. On Linux/macOS, the interpreter path is `.venv/bin/python`.

| Environment Variable                  | Default                                   |
| ------------------------------------- | ----------------------------------------- |
| `LOCAL_TIMEZONE`                      | `Europe/Kyiv`                             |
| `NIGHT_START_HOUR` / `NIGHT_END_HOUR` | `0` / `6` (integer hours, end exclusive)  |
| `CHANNELS`                            | Both requested usernames, comma-separated |
| `NOTIFY_RESTORED`                     | `false` (accepts `true` or `false`)       |
| `STATE_BACKEND`                       | `file`; workflow uses `github`            |
| `STATE_FILE`                          | `.state/state.json` (local backend)       |
| `STATE_BRANCH`                        | `svitlobot-state` (GitHub backend)        |

The GitHub backend also needs the automatically provided `GITHUB_REPOSITORY`,
`GITHUB_SHA`, and the workflow-injected `GITHUB_TOKEN`. Do not use this backend
locally unless you deliberately configure equivalent GitHub access.

## Troubleshooting

Logs intentionally include exception classes rather than raw network errors,
which could contain private data. A failed job is not itself an outage email.
Enable GitHub Actions failure notifications for your account.

- `SMTPAuthenticationError`: check app password, sender account, and 2-Step
  Verification. Some Google accounts cannot use SMTP app passwords.
- `SMTPRecipientsRefused` / other SMTP errors: check the recipient and Gmail
  sending limits; inspect the Gmail account for security notices. Accepted
  SMTP delivery is not proof that the email reached your inbox.
- `HTTPError`, `Timeout`, or `ConnectionError`: Telegram/GitHub may be blocked,
  rate-limited, or unavailable. For state errors, check workflow write
  permission and state-branch rules. GETs have limited retries; SMTP and state
  writes do not retry immediately to reduce duplicate-delivery risk.
- `ValueError`, `JSONDecodeError`, or `ZoneInfoNotFoundError`: inspect settings,
  state schema, installed `tzdata`, and whether Telegram's markup changed.
  Missing credentials also produce `ValueError`; verify all three secrets.
- No runs: check the default branch, schedule enablement, account Actions
  limits, and GitHub's possible queue delays. A manual `dry-run` helps separate
  scheduling problems from scraping problems.

References: [GitHub schedule behavior and timezone support](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[Google App Passwords](https://support.google.com/accounts/answer/185833).