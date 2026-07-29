# Job Radar Commands

## PowerShell setup

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Main commands

```powershell
python -m src.main --mode web
python -m src.main
python -m src.main --mode boards
python -m src.main --mode digest --digest-db state/gha-boards.db
python -m src.main --mode missed --digest-db state/gha-boards.db
python -m src.main --test-notify
python -m src.main --health-check
```

## Alert cadence (digest model)

Scans run every 2 hours and **store** matches without emailing (`--no-notify`).
Emails are batched: the `digest` mode collects every stored-but-not-yet-alerted
YES/MAYBE job across **all** scanner databases, sends a single consolidated
email, and stamps them so they are never re-sent.

The `digest.yml` workflow runs this 3x/day (every 8 hours) as one job covering
both databases (`state/gha-jobs.db` primary, `state/gha-boards.db` via
`--digest-db`), so the hard cap is **one email per run — max 3 emails per day**.
A role found by both scanners is emailed once and stamped in both databases.

Send times are anchored to US Eastern via `cron: "0 3,11,19 * * *"` (cron is
always UTC):

| UTC | EDT (Mar–Nov) | EST (Nov–Mar) |
|-----|---------------|---------------|
| 03:00 | 11:00 PM (prev day) | 10:00 PM (prev day) |
| 11:00 | 7:00 AM | 6:00 AM |
| 19:00 | 3:00 PM | 2:00 PM |

GitHub cron does not follow daylight saving, so the wall-clock times shift by
an hour twice a year. The 8-hour spacing is unaffected. To keep the EDT times
year-round, change the hours to `4,12,20` when EST begins.

## Missed-roles audit (safety net)

`missed.yml` runs `--mode missed` every 3 days (`cron: "0 15 */3 * *"`, 11am
EDT) and emails anything that was stored but never actually sent, under the
subject `[Job Radar] Missed roles — ...` so it is distinguishable from a
normal digest. It sends nothing when nothing was missed.

It exists because the digest can leave matches pending indefinitely:

- `--notify-yes-only` holds a window containing only MAYBE matches until some
  future YES arrives — the most common cause;
- a failed delivery leaves that batch pending for retry;
- any future bug in the repost/suppression logic.

The audit deliberately does **not** apply the yes-only gate, and only reports
matches older than `--missed-min-age-hours` (default 24) so roles the next
digest will deliver normally are left alone. Reported roles are stamped, so
nothing is reported twice.

## Operating model

- `public-export` is the public GitHub Actions repo and the remote automation source of truth.
- `job-radar` is the local development repo.
- Public GitHub board sweeps run every 2 hours with no cooldown.
- Local dashboard launches can still compare `public-export/state/gha-jobs.db` with the sibling `job-radar` DB and surface which one is active.

## Useful variants

```powershell
python -u -m src.main --mode web
python -m src.main --dry-run --verbose
python -m src.main --mode boards --dry-run --verbose
python -m src.main --mode boards --boards-batch-size 50
python -m src.main --mode boards --boards-run-until-wrap
python -m src.main --mode web --web-port 8080
```

## Open the web UI

```text
http://127.0.0.1:8080
```

## Base resume file

Edit this file to change the base resume used for generated drafts:

```text
data/resume/base_resume.md
```

## Config checks

```powershell
python -m src.main --test-notify
python -m src.main --mode web
```

## If dependencies are missing

```powershell
python -m pip install -r requirements.txt
```

## Weekly health check

`health.yml` runs `--health-check` every Monday at 9am ET (`cron: "0 13 * * 1"`).
Beyond job counts it reports **alert delivery**, so a digest that silently
stops emailing is visible rather than looking like a quiet week:

```
-- EMAIL DELIVERY --
OK — alert delivery is healthy
Emailed last 24h    : 74
Emailed last 7d     : 512
Not yet emailed     : 3
Last alert sent     : 2026-07-28T20:10:52
Oldest waiting      : none waiting
```

It raises a warning (and says so in the subject line) when no alert has gone
out in over 24h — the digest runs every 8h, so that indicates a stall — or
when a match has been waiting more than 4 days, which the missed-roles audit
should already have swept up. Delivery figures span every scanner database
via `--digest-db`.
