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
python -m src.main --mode rescore
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

## Rescoring stored jobs

A job's score is written **once**, when a scanner first stores it.
`mark_job_seen` re-scores on `ON CONFLICT`, so a listing that is still live on
its board catches up the next time that board is swept — but only then. A
listing that has been taken down, or whose board is sitting in the empty-board
cooldown (up to 30 days), keeps whatever score the code produced on the day it
was found.

That matters because the digest selects on the **stored label**. A role that
would qualify under today's rules but was stored as `no` is invisible to it
forever, and nothing in the pipeline ever revisits the row.

Every stored job carries a `scoring_version`. `--mode rescore` walks the rows
below `SCORING_VERSION`, re-runs the current scoring pipeline against the
stored description, and writes the fresh score back:

```powershell
python -m src.main --mode rescore                      # drain the backlog (1500/pass)
python -m src.main --mode rescore --dry-run            # report only, write nothing
python -m src.main --mode rescore --rescore-all        # ignore the version stamp
python -m src.main --track software --mode rescore     # software DB, software rules
```

`alerted_at` is deliberately left alone. A row that flips `no` → `yes`/`maybe`
simply becomes pending and goes out with the next digest through the normal
path; a row that flips the other way is corrected without un-sending anything.
A job that cannot be scored is still stamped, so one bad row cannot block the
backlog from draining.

**When you change scoring logic, bump `SCORING_VERSION` in
`src/scoring_policy.py`** — new or reworded keywords, a changed cap or
threshold, a new exclusion rule. Leave it alone for changes that cannot move a
score. Nothing else marks stored rows as out of date.

`rescore.yml` runs on every push to `main` that touches a scoring file, weekly
on Sunday 05:10 UTC as a safety net, and on demand via `workflow_dispatch`
(with a `rescore_all` toggle). It covers all three databases — both data DBs on
the data track, `gha-software.db` on the software track — and joins the shared
`job-radar-shared-state` concurrency group. It never sends email itself.

The weekly health check reports the backlog as **Awaiting rescore** and raises
a delivery warning when it is non-zero.

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

## Software Developer track (0-3 years)

A completely separate track for early-career software roles. It shares the
board scrapers but nothing else:

| | Data flow | Software track |
|---|---|---|
| Database | `state/gha-jobs.db`, `state/gha-boards.db` | `state/gha-software.db` |
| Workflow | `boards/priority/main` + `digest` | `software.yml` |
| Schedule | digest 03/11/19 UTC | 01:25 / 09:25 / 17:25 UTC |
| Email subject | `[Job Radar Digest]` | `[Job Radar SWE]` |

```powershell
python -m src.main --track software --mode boards --no-notify
python -m src.main --track software --mode digest --subject-prefix "[Job Radar SWE]"
```

`--track software` swaps the classifier's keyword domain and the target-role
list. It defaults to `data`, so every existing command is unchanged. `main.py`
refuses to start the software track against `gha-jobs.db` or `gha-boards.db`,
so the two sets of results can never mix.

### Experience gate

The headline filter is 0-3 years:

- **4+ years stated → blocked** (score forced to 0);
- **1-3 years stated → ranked highest** (`EXPLICIT_JUNIOR_BONUS`);
- **nothing stated → included, ranked below explicit 0-3** (most junior
  postings omit the number, so excluding them would lose real roles).

New Grad / University Grad / Entry Level / `Engineer I` titles get a small
boost. Internships and co-ops stay excluded, as in the data flow. Titles that
merely contain "engineer" (civil, mechanical, sales, business development) are
rejected outright.

### Why this track sends MAYBE matches

Scores here are capped by resume-fit, which is measured against
`data/resume/base_resume.md` — a data-science resume. Software requirements
find little matching evidence, so roles rarely reach the `yes` threshold. The
software digest therefore does **not** use `--notify-yes-only`; that gate would
silently suppress every alert. To raise the scores, add a software-oriented
`data/resume/candidate_evidence.local.md` (gitignored) — the evaluator prefers
it over the tracked resume.

## Stale board backoff

Boards that keep returning "0 jobs returned" are retried on a doubling
interval instead of a flat week: 7 days, then 14, then 28, capped at 30.

Around 300 boards belong to companies that have left their ATS — their
careers pages render zero jobs and the API agrees. A flat weekly retry
re-fetched all of them on every sweep forever, roughly 27% of the sweep spent
on boards that had returned nothing for months. Backing off cuts those
fetches by about 73%.

They are deliberately **not** marked dead. A dead board is skipped forever,
and companies do pause hiring and come back, so the cap guarantees every
board is still rechecked at least monthly.
