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
python -m src.main --track coordinator --mode boards --no-notify
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

## Project Coordinator track

A third completely separate track, for project/program coordination roles. It
shares the board scrapers but nothing else:

| | Data flow | Software track | Coordinator track |
|---|---|---|---|
| Database | `state/gha-jobs.db`, `state/gha-boards.db` | `state/gha-software.db` | `state/gha-coordinator.db` |
| Workflow | `boards/priority/main` + `digest` | `software.yml` | `coordinator.yml` |
| Schedule (UTC) | digest 02:40 / 10:40 / 18:40 | 01:25 / 09:25 / 17:25 | 05:50 / 13:50 / 21:50 |
| Email subject | `[Job Radar Digest]` | `[Job Radar SWE]` | `[Job Radar PC]` |
| Concurrency group | `job-radar-shared-state` | `job-radar-software` | `job-radar-coordinator` |

```powershell
python -m src.main --track coordinator --mode boards --no-notify
python -m src.main --track coordinator --mode digest --subject-prefix "[Job Radar PC]"
```

### Why it needed its own track

"coordinator" appears in **none** of the data or software keyword lists, so
`classify()` scored every Project Coordinator posting 0 / `no`, and the title
gate in `main.py` drops `no` titles before `mark_job_seen` ever runs. The roles
were never stored, never scored, never emailed — and because rejected titles
are not logged, they left no trace anywhere.

### Keyword policy

- **STRONG (90)** — the coordinator family proper: project/program
  coordinator, project administrator, project analyst, PMO analyst, project
  scheduler, operations coordinator.
- **WEAK (55)** — a step up: project manager, program manager, scrum master,
  implementation specialist. These usually surface as `maybe`.
- **Entry-level markers** (`I`, `Associate`, `Junior`, `Entry Level`) add +8.
- **Rejected outright** — clinical/trade roles that share the noun but not the
  job (patient care, nursing, HVAC, CDL), and titles owned by the other two
  tracks.

Seniority follows the **data** track's policy, not the software one: a
senior/lead marker caps the score at `maybe` rather than rejecting, and
director/VP titles are rejected. Coordinator postings are junior by nature, and
the shared experience gate in `evaluation.py` already blocks any description
asking for more than three years, so a second hard title filter would only lose
borderline openings worth seeing.

### Track isolation

The three tracks keep separate databases with **no shared dedup**, so a title
matching two of them would be emailed twice. `tests/test_coordinator_track.py`
asserts the keyword lists are disjoint and that every sample title scores on at
most one track. Two candidate keywords — `business analyst` and
`operations analyst` — were deliberately left out of `COORDINATOR_WEAK` for
exactly this reason; both already belong to the data track.

`main.py` refuses to start any non-default track against another track's
database, so the three sets of results can never mix:

```
Refusing to run the coordinator track against state/gha-jobs.db — that database
belongs to another track. Set DB_PATH to state/gha-coordinator.db so the other
flows are untouched.
```

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
