"""Lightweight web UI for browsing jobs, toggling features, and generating resumes."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote
from wsgiref.simple_server import make_server

from .config import Config
from .database import Database
from .evaluation import evaluate_job
from .job_intelligence import extract_workday_req_id
from .scoring_policy import calibrate_thresholds
from .resume_builder import generate_resume_packet
from .sources.base import is_us_location, remote_scope_status

PIPELINE_STATUSES = [
    "new",
    "shortlisted",
    "resume_generated",
    "applied",
    "interview",
    "onsite",
    "offer",
    "screen_reject",
    "ghosted",
    "rejected",
    "archived",
]
GRADE_SCORES = {
    "A": 6,
    "B": 5,
    "C": 4,
    "D": 3,
    "E": 2,
    "F": 1,
}

RECENT_JOB_DAYS = 1
DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
)

log = logging.getLogger(__name__)
STATE_DB_FILENAMES = ("gha-jobs.db", "gha-boards.db")


def _resolve_db_path(repo_root: str, raw_path: str) -> Path:
    candidate = Path(os.path.expanduser(raw_path))
    if candidate.is_absolute():
        return candidate
    base = Path(repo_root) if repo_root else Path.cwd()
    return (base / candidate).resolve()


def _state_db_paths(root: Path) -> list[Path]:
    return [(root / "state" / name).resolve() for name in STATE_DB_FILENAMES]


def _db_snapshot(path: Path) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "path": path,
        "exists": path.exists(),
        "healthy": False,
        "total_jobs": 0,
        "recent_jobs_24h": 0,
        "recent_first_seen_24h": 0,
        "last_seen": "",
        "fresh_ts": 0.0,
    }
    if not path.exists():
        return snapshot
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        snapshot["total_jobs"] = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        snapshot["recent_jobs_24h"] = int(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE last_seen >= datetime('now','-1 day')").fetchone()[0]
        )
        snapshot["recent_first_seen_24h"] = int(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE first_seen >= datetime('now','-1 day')").fetchone()[0]
        )
        last_seen = str(conn.execute("SELECT MAX(last_seen) FROM jobs").fetchone()[0] or "")
        snapshot["last_seen"] = last_seen
        parsed = _parse_datetime(last_seen)
        snapshot["fresh_ts"] = parsed.timestamp() if parsed else 0.0
        snapshot["healthy"] = True
        return snapshot
    except sqlite3.Error:
        return snapshot
    finally:
        conn.close()


def _dashboard_source_label(repo_root: str, path: Path) -> str:
    root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    resolved = path.resolve()
    public_export = (root / "public-export").resolve()
    if resolved in set(_state_db_paths(public_export)):
        return "public-export"
    if root.name.lower() == "public-export" and resolved in set(_state_db_paths(root)):
        return "public-export"
    if resolved in set(_state_db_paths(root)):
        return "local"
    if "dashboard-merged-" in resolved.name:
        return "merged"
    return resolved.parent.name or "local"


def _copy_db_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for sidecar in (
        dst,
        dst.with_name(dst.name + "-wal"),
        dst.with_name(dst.name + "-shm"),
    ):
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:
            pass
    shutil.copy2(src, dst)


def _merge_jobs_into(base_path: Path, overlay_path: Path) -> None:
    base = sqlite3.connect(str(base_path))
    overlay = sqlite3.connect(str(overlay_path))
    try:
        base.row_factory = sqlite3.Row
        overlay.row_factory = sqlite3.Row
        columns = [str(row["name"]) for row in overlay.execute("PRAGMA table_info(jobs)").fetchall()]
        if not columns:
            return
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        select_sql = f"SELECT {column_sql} FROM jobs"
        upsert_sql = f"INSERT OR REPLACE INTO jobs ({column_sql}) VALUES ({placeholders})"
        for row in overlay.execute(select_sql).fetchall():
            existing = base.execute("SELECT last_seen FROM jobs WHERE key=?", (row["key"],)).fetchone()
            if existing is None or str(row["last_seen"] or "") > str(existing["last_seen"] or ""):
                base.execute(upsert_sql, tuple(row[col] for col in columns))
        base.commit()
    finally:
        overlay.close()
        base.close()


def _safe_merge_dashboard_db(
    repo_root: str,
    snaps: list[dict[str, object]],
    *,
    strategy_label: str,
) -> tuple[Path, str]:
    ordered = sorted(
        snaps,
        key=lambda snap: (
            float(snap["fresh_ts"]),
            int(snap["recent_jobs_24h"]),
            int(snap["total_jobs"]),
        ),
        reverse=True,
    )
    base_snap = ordered[0]
    overlays = ordered[1:]
    merge_key = "|".join(
        f"{Path(snap['path']).name}:{int(float(snap['fresh_ts']))}:{int(snap['total_jobs'])}" for snap in ordered
    )
    merge_hash = hashlib.sha1(merge_key.encode("utf-8")).hexdigest()[:12]
    merged_name = f"dashboard-merged-{merge_hash}.db"
    merged_path = (Path(repo_root) / "state" / merged_name).resolve() if repo_root else Path(base_snap["path"])
    try:
        _copy_db_file(Path(base_snap["path"]), merged_path)
        for overlay_snap in overlays:
            _merge_jobs_into(merged_path, Path(overlay_snap["path"]))
        return merged_path, strategy_label
    except (sqlite3.Error, OSError) as exc:
        base_path = Path(base_snap["path"])
        log.warning(
            "Dashboard DB merge failed for %s: %s. Falling back to %s.",
            base_path,
            exc,
            base_path,
        )
        return base_path, f"{strategy_label}-fallback"


def _select_dashboard_db(repo_root: str, db_path: str) -> tuple[Path, str]:
    primary = _resolve_db_path(repo_root, db_path)
    root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    candidate_paths: list[Path] = [primary]
    candidate_paths.extend(path for path in _state_db_paths(root) if path != primary)
    nested_public_export = (root / "public-export").resolve()
    candidate_paths.extend(path for path in _state_db_paths(nested_public_export) if path != primary)
    if root.name.lower() == "public-export":
        candidate_paths.extend(path for path in _state_db_paths(root.parent.resolve()) if path != primary)
    deduped_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for path in candidate_paths:
        if path in seen_paths:
            continue
        deduped_paths.append(path)
        seen_paths.add(path)

    existing = [snap for snap in (_db_snapshot(path) for path in deduped_paths) if snap["exists"] and bool(snap.get("healthy"))]
    if not existing:
        return primary, "primary-missing"

    grouped: dict[str, list[dict[str, object]]] = {}
    for snap in existing:
        label = _dashboard_source_label(repo_root, Path(snap["path"]))
        grouped.setdefault(label, []).append(snap)

    def _collapse_group(label: str) -> dict[str, object] | None:
        snaps = grouped.get(label, [])
        if not snaps:
            return None
        if len(snaps) == 1:
            return snaps[0]
        merged_path, _ = _safe_merge_dashboard_db(repo_root, snaps, strategy_label=f"{label}-internal")
        merged_snap = _db_snapshot(merged_path)
        if merged_snap["exists"] and bool(merged_snap.get("healthy")):
            return merged_snap
        return snaps[0]

    public_candidate = _collapse_group("public-export")
    local_candidate = _collapse_group("local")

    if public_candidate and local_candidate:
        if int(public_candidate["recent_jobs_24h"]) > 0 and int(local_candidate["recent_jobs_24h"]) > 0:
            return _safe_merge_dashboard_db(
                repo_root,
                [public_candidate, local_candidate],
                strategy_label="merged-public+local",
            )
        return Path(public_candidate["path"]), "public-truth"

    if public_candidate:
        return Path(public_candidate["path"]), "public-truth"

    existing.sort(
        key=lambda snap: (
            float(snap["fresh_ts"]),
            int(snap["recent_jobs_24h"]),
            int(snap["total_jobs"]),
        ),
        reverse=True,
    )
    best = existing[0]
    if len(existing) == 1:
        return Path(best["path"]), "single"
    other = existing[1]
    if int(best["recent_jobs_24h"]) > 0 and int(other["recent_jobs_24h"]) > 0:
        return _safe_merge_dashboard_db(
            repo_root,
            [best, other],
            strategy_label="merged",
        )
    return Path(best["path"]), "freshest"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return cleaned.strip("-") or "job"


def _parse_datetime(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _is_recent_job(job: dict, *, max_days: int = RECENT_JOB_DAYS) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    posted_dt = _parse_datetime(job.get("posted", ""))
    if posted_dt is not None:
        return posted_dt >= cutoff
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if first_seen_dt is not None:
        return first_seen_dt >= cutoff
    return True


def _is_recent_first_seen(job: dict, *, max_days: int = RECENT_JOB_DAYS) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if first_seen_dt is not None:
        return first_seen_dt >= cutoff
    return True


def _job_sort_dt(job: dict) -> datetime:
    posted_dt = _parse_datetime(job.get("posted", ""))
    if posted_dt is not None:
        return posted_dt
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if first_seen_dt is not None:
        return first_seen_dt
    return datetime.min.replace(tzinfo=timezone.utc)


def _job_sort_ts(job: dict) -> float:
    dt = _job_sort_dt(job)
    if dt.year <= 1970:
        return 0.0
    return dt.timestamp()


def _passes_freshness(job: dict, *, days_raw: str, max_days: int) -> bool:
    if days_raw.startswith("seen_"):
        return _is_recent_first_seen(job, max_days=max_days)
    if (job.get("source") or "").strip().lower() == "linkedin":
        return _is_recent_job(job, max_days=1)
    if days_raw == "all":
        return True
    return _is_recent_job(job, max_days=max_days)


def _days_value_to_max_days(days_raw: str) -> int:
    normalized = (days_raw or str(RECENT_JOB_DAYS)).strip().lower()
    if normalized == "all":
        return RECENT_JOB_DAYS
    if normalized.startswith("seen_"):
        normalized = normalized.split("_", 1)[1]
    try:
        return max(int(normalized), 1)
    except ValueError:
        return RECENT_JOB_DAYS


def _source_match(job: dict, source: str) -> bool:
    if not source or source == "all":
        return True
    return (job.get("source") or "").strip().lower() == source


def _sort_jobs(jobs: list[dict], sort_by: str) -> None:
    if sort_by == "oldest":
        jobs.sort(key=lambda job: (_job_sort_dt(job), job["score"]))
        return
    if sort_by == "score":
        jobs.sort(key=lambda job: (int(job.get("score") or 0), _job_sort_dt(job)), reverse=True)
        return
    if sort_by == "rating":
        jobs.sort(
            key=lambda job: (
                GRADE_SCORES.get((job.get("grade") or "F").upper(), 0),
                int(job.get("score") or 0),
                _job_sort_dt(job),
            ),
            reverse=True,
        )
        return
    if sort_by == "source":
        jobs.sort(
            key=lambda job: (
                (job.get("source") or "").strip().lower(),
                -int(job.get("score") or 0),
                -_job_sort_ts(job),
            )
        )
        return
    jobs.sort(key=lambda job: (_job_sort_dt(job), job["score"]), reverse=True)


def _queue_match(job: dict, queue: str, status: str) -> bool:
    pipeline_status = (job.get("pipeline_status") or "new").strip().lower()
    if status and status != "all" and pipeline_status != status:
        return False
    if queue == "all":
        return True
    if queue == "active":
        return pipeline_status not in {"archived", "rejected", "screen_reject", "ghosted"}
    if queue == "actionable":
        return pipeline_status in {"new", "shortlisted", "resume_generated", "applied", "interview", "onsite"}
    return True


def _location_allowed_for_review(location: str, *, require_us_location: bool) -> bool:
    if not require_us_location:
        return True
    if is_us_location(location):
        return True
    return remote_scope_status(location) == "unspecified"


def _feature_defaults(cfg: Config) -> dict[str, bool]:
    return {
        "scanner_main": cfg.features.scanner_main,
        "scanner_boards": cfg.features.scanner_boards,
        "notifications": cfg.features.notifications,
        "manual_jd": cfg.features.manual_jd,
        "resume_generation": cfg.features.resume_generation,
    }


def _job_structured(job: dict) -> dict[str, object]:
    raw = (job.get("structured_json") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _structured_signal_pills(job: dict) -> str:
    data = _job_structured(job)
    pills: list[str] = []
    if not (job.get("description") or "").strip():
        pills.append("<span class=\"pill\">No JD</span>")
    remote_mode = str(data.get("remote_mode") or "").strip()
    if remote_mode:
        pills.append(f"<span class=\"pill\">{escape(remote_mode.title())}</span>")
    salary_min = data.get("salary_min")
    salary_max = data.get("salary_max")
    salary_period = str(data.get("salary_period") or "year")
    if isinstance(salary_min, int) and isinstance(salary_max, int):
        pills.append(f"<span class=\"pill\">${salary_min:,} - ${salary_max:,}/{escape(salary_period)}</span>")
    yoe_min = data.get("years_experience_min")
    yoe_max = data.get("years_experience_max")
    if isinstance(yoe_min, int):
        if isinstance(yoe_max, int):
            pills.append(f"<span class=\"pill\">{yoe_min}-{yoe_max} yrs</span>")
        else:
            pills.append(f"<span class=\"pill\">{yoe_min}+ yrs</span>")
    if data.get("linkedin_verified"):
        pills.append("<span class=\"pill\">Verified</span>")
    if data.get("visa_sponsorship") is True:
        pills.append("<span class=\"pill\">Visa sponsor</span>")
    elif data.get("visa_sponsorship") is False:
        pills.append("<span class=\"pill\">No visa sponsor</span>")
    if data.get("security_clearance"):
        pills.append("<span class=\"pill\">Clearance req</span>")
    if data.get("citizenship_requirement"):
        pills.append("<span class=\"pill\">Citizenship req</span>")
    employment_type = str(data.get("employment_type") or "").strip()
    if employment_type:
        pills.append(f"<span class=\"pill\">{escape(employment_type.title())}</span>")
    return "".join(pills) or "<span class=\"muted\">No extracted signals.</span>"


def _board_inventory_total(cfg: Config) -> int:
    try:
        from .main import _resolve_boards_csv
    except Exception:
        return 0
    try:
        path = _resolve_boards_csv(cfg.boards.csv)
    except Exception:
        return 0
    total = 0
    with open(path, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            company = (row.get("company_name") or row.get("company") or "").strip()
            platform = (row.get("platform") or "").strip().lower()
            url = (row.get("board_url") or row.get("url") or "").strip()
            ok_val = (row.get("ok") or "").strip().lower()
            if ok_val and ok_val not in ("true", "1", "yes"):
                continue
            if company and platform and url:
                total += 1
    return total


def _workday_legacy_duplicate_keys(jobs: list[dict]) -> set[str]:
    by_req: dict[str, dict[str, set[str]]] = {}
    for job in jobs:
        if (job.get("source") or "").strip().lower() != "workday":
            continue
        key = str(job.get("key") or "")
        req_id = extract_workday_req_id(key) or extract_workday_req_id(str(job.get("url") or ""))
        if not req_id:
            continue
        bucket = by_req.setdefault(req_id, {"url_keys": set(), "canonical_keys": set()})
        if ":url:" in key:
            bucket["url_keys"].add(key)
        else:
            bucket["canonical_keys"].add(key)
    hidden: set[str] = set()
    for bucket in by_req.values():
        if bucket["canonical_keys"] and bucket["url_keys"]:
            hidden.update(bucket["url_keys"])
    return hidden


def _canonical_workday_key_for_hidden(hidden_key: str, jobs: list[dict]) -> str:
    req_id = extract_workday_req_id(hidden_key)
    if not req_id:
        return ""
    for job in jobs:
        key = str(job.get("key") or "")
        if key == hidden_key:
            continue
        if (job.get("source") or "").strip().lower() != "workday":
            continue
        if ":url:" in key:
            continue
        if extract_workday_req_id(key) == req_id:
            return key
    return ""


def _layout(title: str, body: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f4ed;
      --panel: #fffdf8;
      --ink: #1f2937;
      --muted: #6b7280;
      --line: #ddd6c7;
      --good: #166534;
      --warn: #b45309;
      --bad: #991b1b;
      --accent: #0f766e;
    }}
    body {{ margin: 0; font-family: Georgia, 'Times New Roman', serif; background: var(--bg); color: var(--ink); }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    .nav {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
    .nav a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; margin-bottom: 18px; }}
    .grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }}
    .hero {{ display: grid; grid-template-columns: 1.8fr 1fr; gap: 18px; align-items: start; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 16px 0; }}
    .stat {{ background: #fbfaf6; border: 1px solid var(--line); border-radius: 12px; padding: 14px; }}
    .stat strong {{ display: block; font-size: 1.6rem; line-height: 1.1; }}
    .stat span {{ color: var(--muted); font-size: 0.92rem; }}
    .helper-list {{ margin: 10px 0 0; padding-left: 18px; color: var(--muted); }}
    .table-wrap {{ overflow-x: auto; }}
    .muted {{ color: var(--muted); }}
    .score {{ font-weight: 700; }}
    .yes {{ color: var(--good); }}
    .maybe {{ color: var(--warn); }}
    .no {{ color: var(--bad); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    textarea, input[type=text], input[type=url], select {{
      width: 100%; box-sizing: border-box; border: 1px solid var(--line);
      border-radius: 10px; padding: 10px 12px; font: inherit; background: #fff;
    }}
    textarea {{ min-height: 220px; resize: vertical; }}
    button {{
      background: var(--accent); color: white; border: 0; border-radius: 999px;
      padding: 10px 16px; font: inherit; cursor: pointer; font-weight: 700;
    }}
    .pill {{
      display: inline-block; border: 1px solid var(--line); border-radius: 999px;
      padding: 4px 10px; margin-right: 6px; margin-bottom: 6px; background: #fff;
    }}
    .split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
    pre {{
      white-space: pre-wrap; word-break: break-word; background: #fbfaf6;
      border: 1px solid var(--line); border-radius: 10px; padding: 14px;
    }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
    .actions label {{ min-width: 140px; flex: 1 1 140px; }}
    .scan-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }}
    .scan-card {{ background: #fbfaf6; border: 1px solid var(--line); border-radius: 12px; padding: 14px; }}
    .scan-card h3 {{ margin: 0 0 8px; font-size: 1rem; }}
    .scan-card p {{ margin: 0; color: var(--muted); font-size: 0.95rem; }}
    .danger-card {{ background: #fff7ed; border-color: #fdba74; }}
    .danger-button {{ background: #b45309; }}
    .confirm-line {{ display: flex; align-items: center; gap: 8px; margin-top: 12px; color: var(--muted); font-size: 0.94rem; }}
    .confirm-line input[type=checkbox] {{ width: auto; }}
    .button-link {{
      display: inline-block; background: var(--accent); color: white; border: 0; border-radius: 999px;
      padding: 10px 16px; text-decoration: none; font-weight: 700;
    }}
    .artifact-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; }}
    .artifact-meta {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }}
    @media (max-width: 880px) {{
      .grid, .split, .hero, .scan-grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .wrap {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="nav">
      <a href="/">Jobs</a>
      <a href="/health">Health</a>
      <a href="/boards">Board Health</a>
      <a href="/manual-jd">Paste JD</a>
      <a href="/settings">Feature Switchboard</a>
    </div>
    {body}
  </div>
  <script>
    window.copyText = function (id) {{
      const node = document.getElementById(id);
      if (!node) return;
      const text = node.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text);
        return;
      }}
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }};
    document.addEventListener("change", function (event) {{
      const target = event.target;
      if (!target || target.tagName !== "SELECT") return;
      const form = target.closest("form[data-autosubmit='true']");
      if (!form) return;
      if (typeof form.requestSubmit === "function") {{
        form.requestSubmit();
      }} else {{
        form.submit();
      }}
    }});
  </script>
</body>
</html>"""
    return html.encode("utf-8")


def serve_web(
    cfg: Config,
    db: Database,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    repo_root: str = "",
    auto_pull_interval_seconds: int = 300,
) -> None:
    defaults = _feature_defaults(cfg)
    db_lock = threading.RLock()
    scan_lock = threading.Lock()
    last_repo_pull_monotonic = 0.0
    active_dashboard_db_path = str(_resolve_db_path(repo_root, cfg.database.path))
    active_dashboard_db_meta: dict[str, object] = {
        "path": active_dashboard_db_path,
        "strategy": "configured",
        "source": _dashboard_source_label(repo_root, Path(active_dashboard_db_path)),
        "last_seen": "",
        "recent_jobs_24h": 0,
        "recent_first_seen_24h": 0,
        "total_jobs": 0,
    }
    scan_state: dict[str, object] = {
        "running": False,
        "mode": "",
        "message": "No scan has been started from the web UI yet.",
        "started_at": "",
        "finished_at": "",
    }

    def _scan_snapshot() -> dict[str, object]:
        with scan_lock:
            return dict(scan_state)

    def _open_worker_db() -> Database:
        return Database(cfg.database.path)

    def _set_scan_state(**updates) -> None:
        with scan_lock:
            scan_state.update(updates)

    def _replace_dashboard_db() -> None:
        nonlocal db, active_dashboard_db_path, active_dashboard_db_meta
        replacement_path, strategy = _select_dashboard_db(repo_root, cfg.database.path)
        resolved_path = str(replacement_path)
        snapshot = _db_snapshot(replacement_path)
        active_dashboard_db_meta = {
            "path": resolved_path,
            "strategy": strategy,
            "source": _dashboard_source_label(repo_root, replacement_path),
            "last_seen": snapshot.get("last_seen", ""),
            "recent_jobs_24h": snapshot.get("recent_jobs_24h", 0),
            "recent_first_seen_24h": snapshot.get("recent_first_seen_24h", 0),
            "total_jobs": snapshot.get("total_jobs", 0),
        }
        if resolved_path == active_dashboard_db_path:
            return
        replacement = Database(resolved_path)
        with db_lock:
            old_db = db
            db = replacement
            active_dashboard_db_path = resolved_path
        try:
            old_db.close()
        except Exception:
            pass
        log.info("Dashboard DB switched to %s (%s).", resolved_path, strategy)

    def _maybe_pull_repo() -> None:
        nonlocal last_repo_pull_monotonic
        if not repo_root or auto_pull_interval_seconds <= 0:
            return
        now = time.monotonic()
        if now - last_repo_pull_monotonic < auto_pull_interval_seconds:
            return
        last_repo_pull_monotonic = now
        if _scan_snapshot().get("running"):
            return
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            log.warning("Dashboard auto-pull skipped: git status failed (%s).", exc)
            return
        if status.returncode != 0 or status.stdout.strip():
            return
        try:
            pull = subprocess.run(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            log.warning("Dashboard auto-pull failed: %s", exc)
            return
        output = (pull.stdout or pull.stderr or "").strip()
        if pull.returncode != 0:
            log.warning("Dashboard auto-pull failed: %s", output or f"git exited with {pull.returncode}")
            return
        if "Already up to date." in output or "Already up-to-date." in output:
            _replace_dashboard_db()
            return
        log.info("Dashboard auto-pull refreshed repo state.")
        _replace_dashboard_db()

    def _start_scan(mode: str) -> bool:
        current = _scan_snapshot()
        if current.get("running"):
            return False

        started_at = datetime.now(timezone.utc).isoformat()
        batch_size = max(int(cfg.boards.batch_size or 0), 1)
        if mode == "boards":
            cursor_db = _open_worker_db()
            cursor = worker_db_cursor = cursor_db.get_cursor("boards_main")
            try:
                worker_db_cursor = int(worker_db_cursor or 0)
            except Exception:
                worker_db_cursor = 0
            finally:
                cursor_db.close()
            message = (
                f"Started next board batch at {started_at}. "
                f"It will scan up to {batch_size} boards from the current cursor ({worker_db_cursor})."
            )
        elif mode == "all":
            cursor_db = _open_worker_db()
            cursor = worker_db_cursor = cursor_db.get_cursor("boards_main")
            try:
                worker_db_cursor = int(worker_db_cursor or 0)
            except Exception:
                worker_db_cursor = 0
            finally:
                cursor_db.close()
            message = (
                f"Started full board sweep at {started_at}. "
                f"It resumes from the current cursor ({worker_db_cursor}) and wraps until all boards are covered."
            )
        else:
            message = f"Started main sources scan at {started_at}."
        _set_scan_state(
            running=True,
            mode=mode,
            started_at=started_at,
            finished_at="",
            message=message,
        )

        def _worker() -> None:
            from .main import _resolve_boards_csv, build_notifier, run_boards, run_main

            worker_db = _open_worker_db()
            notifier = build_notifier(cfg)
            try:
                if mode in {"main", "all"}:
                    run_main(cfg, worker_db, notifier, dry_run=False, no_notify=False, test_notify=False)
                if mode in {"boards", "all"}:
                    boards_csv = _resolve_boards_csv(cfg.boards.csv)
                    run_boards(
                        cfg,
                        worker_db,
                        notifier,
                        boards_csv=boards_csv,
                        batch_size=cfg.boards.batch_size,
                        timeout=cfg.boards.timeout,
                        workers=cfg.boards.workers,
                        dry_run=False,
                        no_notify=False,
                        test_notify=False,
                        run_until_wrap=(mode == "all"),
                        show_live_progress=False,
                    )
                finished_at = datetime.now(timezone.utc).isoformat()
                message = (
                    "Completed main sources + full board sweep from the saved cursor."
                    if mode == "all"
                    else "Completed main source scan."
                    if mode == "main"
                    else "Completed next board batch scan."
                )
                _set_scan_state(
                    running=False,
                    finished_at=finished_at,
                    message=f"{message} Finished at {finished_at}.",
                )
            except Exception as exc:
                finished_at = datetime.now(timezone.utc).isoformat()
                log.exception("Web-triggered scan failed: %s", exc)
                _set_scan_state(
                    running=False,
                    finished_at=finished_at,
                    message=f"Scan failed at {finished_at}: {type(exc).__name__}: {exc}",
                )
            finally:
                worker_db.close()

        thread = threading.Thread(target=_worker, name=f"job-radar-scan-{mode}", daemon=True)
        thread.start()
        return True

    def _read_post(environ) -> dict[str, str]:
        try:
            size = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            size = 0
        raw = environ["wsgi.input"].read(size).decode("utf-8") if size > 0 else ""
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

    def _redirect(start_response, location: str):
        start_response("303 See Other", [("Location", location)])
        return [b""]

    def _html_headers() -> list[tuple[str, str]]:
        return [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
        ]

    def _artifact_content_type(format_name: str) -> str:
        normalized = (format_name or "").strip().lower()
        if normalized == "markdown":
            return "text/markdown; charset=utf-8"
        if normalized == "tex":
            return "application/x-tex; charset=utf-8"
        return "text/plain; charset=utf-8"

    def _packet_link(item: dict) -> str:
        if (item.get("format") or "").strip().lower() == "prompt_packet":
            return f"/packet?id={item['id']}"
        return f"/resume?id={item['id']}"

    def _flags() -> dict[str, bool]:
        return db.get_feature_flags(defaults)

    def _label_thresholds():
        return calibrate_thresholds(db.get_feedback_jobs())

    def _backing_db_paths() -> list[str]:
        root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
        primary = str(_resolve_db_path(repo_root, cfg.database.path))
        paths = [primary]
        paths.extend(str(path) for path in _state_db_paths(root) if str(path) != primary and path.exists())
        public_root = (root / "public-export").resolve()
        paths.extend(str(path) for path in _state_db_paths(public_root) if str(path) != primary and path.exists())
        deduped: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            deduped.append(path)
            seen.add(path)
        return deduped

    def _open_backing_dbs() -> list[tuple[str, Database]]:
        opened: list[tuple[str, Database]] = []
        for path in _backing_db_paths():
            try:
                opened.append((path, Database(path)))
            except Exception as exc:
                log.warning("Skipping backing DB %s for dashboard write: %s", path, exc)
        return opened

    def _evaluate_for_row(job_row: dict, thresholds) -> object:
        return evaluate_job(
            job_row["title"],
            job_row.get("description", ""),
            company=job_row["company"],
            location=job_row["location"],
            source=job_row.get("source", ""),
            require_us_location=cfg.filter.require_us_location,
            yes_threshold=thresholds.yes,
            maybe_threshold=thresholds.maybe,
        )

    def _refresh_and_update_in_db(target_db: Database, seed_job: dict, thresholds) -> tuple[dict, object] | None:
        existing = target_db.get_job(seed_job["key"])
        if existing is None:
            return None
        target_db.refresh_job_intelligence(
            key=seed_job["key"],
            source=seed_job["source"],
            company=seed_job["company"],
            title=seed_job["title"],
            location=seed_job["location"],
            url=seed_job["url"],
            description=seed_job.get("description", ""),
        )
        refreshed = target_db.get_job(seed_job["key"]) or seed_job
        evaluation = _evaluate_for_row(refreshed, thresholds)
        current_json = refreshed.get("evaluation_json") or ""
        if (
            evaluation.fit_summary != (refreshed.get("fit_summary") or "")
            or evaluation.score != refreshed["score"]
            or evaluation.label != refreshed["label"]
            or evaluation.grade != (refreshed.get("grade") or "")
            or evaluation.to_json() != current_json
        ):
            target_db.update_job_evaluation(
                key=seed_job["key"],
                score=evaluation.score,
                label=evaluation.label,
                grade=evaluation.grade,
                evaluation_json=evaluation.to_json(),
                fit_summary=evaluation.fit_summary,
                description=refreshed.get("description", ""),
            )
            refreshed = target_db.get_job(seed_job["key"]) or refreshed
        return refreshed, evaluation

    def _persist_evaluation(job: dict) -> tuple[dict, object]:
        thresholds = _label_thresholds()
        writers = _open_backing_dbs()
        chosen_job = job
        chosen_eval = _evaluate_for_row(job, thresholds)
        try:
            updated_any = False
            for _, writer in writers:
                result = _refresh_and_update_in_db(writer, job, thresholds)
                if result is not None:
                    chosen_job, chosen_eval = result
                    updated_any = True
            if not updated_any:
                result = _refresh_and_update_in_db(db, job, thresholds)
                if result is not None:
                    chosen_job, chosen_eval = result
            return chosen_job, chosen_eval
        finally:
            for path, writer in writers:
                try:
                    writer.close()
                except Exception:
                    log.debug("Failed to close backing DB %s after evaluation persist.", path)

    def _current_view_jobs(filters: dict[str, str]) -> tuple[list[dict], int, list[dict], set[str]]:
        days_raw = (filters.get("days") or str(RECENT_JOB_DAYS)).strip()
        queue = (filters.get("queue") or "active").strip().lower()
        status = (filters.get("status") or "all").strip().lower()
        source = (filters.get("source") or "all").strip().lower()
        sort_by = (filters.get("sort") or "newest").strip().lower()
        days = _days_value_to_max_days(days_raw)

        filtered_jobs = [
            job for job in db.list_jobs_for_board(limit=None)
            if _queue_match(job, queue, status)
            and _passes_freshness(job, days_raw=days_raw, max_days=days)
            and _source_match(job, source)
        ]
        hidden_non_us_count = 0
        if cfg.filter.require_us_location:
            hidden_non_us_count = sum(
                1 for job in filtered_jobs
                if not _location_allowed_for_review(job.get("location", ""), require_us_location=True)
            )
            filtered_jobs = [
                job for job in filtered_jobs
                if _location_allowed_for_review(job.get("location", ""), require_us_location=True)
            ]

        all_jobs = _dedupe_board_jobs(filtered_jobs)
        hidden_workday_keys = _workday_legacy_duplicate_keys(all_jobs)
        jobs = [
            job for job in all_jobs
            if job.get("key") not in hidden_workday_keys
        ]
        _sort_jobs(jobs, sort_by)
        return jobs, hidden_non_us_count, all_jobs, hidden_workday_keys

    def _filtered_jobs_snapshot(filters: dict[str, str], *, limit: int | None = 500) -> list[dict]:
        jobs, _, _, _ = _current_view_jobs(filters)
        if limit is None:
            return jobs
        return jobs[: max(limit, 1)]

    def _batch_refresh(jobs: list[dict]) -> int:
        thresholds = _label_thresholds()
        writers = _open_backing_dbs()
        try:
            for job in jobs:
                if job is None:
                    continue
                wrote = False
                for _, writer in writers:
                    result = _refresh_and_update_in_db(writer, job, thresholds)
                    if result is not None:
                        wrote = True
                if not wrote:
                    _refresh_and_update_in_db(db, job, thresholds)
            return len(jobs)
        finally:
            for path, writer in writers:
                try:
                    writer.close()
                except Exception:
                    log.debug("Failed to close backing DB %s after batch refresh.", path)

    def _dedupe_board_jobs(rows: list[dict]) -> list[dict]:
        best: dict[tuple[str, ...], dict] = {}
        for row in rows:
            canonical_key = (row.get("canonical_key") or "").strip().lower()
            normalized_url = re.sub(r"[?#].*$", "", (row.get("url") or "").strip().lower()).rstrip("/")
            if canonical_key:
                fingerprint = ("canonical", canonical_key)
            elif normalized_url:
                fingerprint = ("url", normalized_url)
            else:
                fingerprint = (
                    "fallback",
                    (row.get("source") or "").strip().lower(),
                    (row.get("company") or "").strip().lower(),
                    (row.get("title") or "").strip().lower(),
                    (row.get("location") or "").strip().lower(),
                )
            current = best.get(fingerprint)
            if current is None:
                best[fingerprint] = row
                continue
            current_desc_len = len((current.get("description") or "").strip())
            row_desc_len = len((row.get("description") or "").strip())
            current_stamp = current.get("last_seen") or current.get("first_seen") or ""
            row_stamp = row.get("last_seen") or row.get("first_seen") or ""
            if row_desc_len > current_desc_len or (row_desc_len == current_desc_len and row_stamp > current_stamp):
                best[fingerprint] = row
        return list(best.values())

    def _jobs_page(start_response, query: dict[str, list[str]]):
        days_raw = (query.get("days") or [str(RECENT_JOB_DAYS)])[-1]
        queue = ((query.get("queue") or ["active"])[-1] or "active").strip().lower()
        status = ((query.get("status") or ["all"])[-1] or "all").strip().lower()
        source = ((query.get("source") or ["all"])[-1] or "all").strip().lower()
        sort_by = ((query.get("sort") or ["newest"])[-1] or "newest").strip().lower()
        rescore_limit_raw = (query.get("rescore_limit") or ["500"])[-1]
        page_message = ((query.get("message") or [""])[-1] or "").strip()
        days = _days_value_to_max_days(days_raw)
        if rescore_limit_raw == "all":
            rescore_limit = None
        else:
            try:
                rescore_limit = max(int(rescore_limit_raw), 1)
            except ValueError:
                rescore_limit = 500
                rescore_limit_raw = "500"
        jobs, hidden_non_us_count, all_jobs, hidden_workday_keys = _current_view_jobs(
            {
                "days": days_raw,
                "queue": queue,
                "status": status,
                "source": source,
                "sort": sort_by,
            }
        )
        source_names = sorted({(job.get("source") or "").strip().lower() for job in jobs if (job.get("source") or "").strip()})
        rows: list[str] = []
        for job in jobs:
            label = escape(job["label"])
            grade = escape(job.get("grade") or "F")
            pipeline_status = escape(job.get("pipeline_status") or "new")
            role_bits = [
                f"<a href=\"/job?key={quote(job['key'], safe='')}\">{escape(job['title'])}</a>",
                f"<div class=\"muted\">{escape(job['company'])}</div>",
            ]
            if int(job.get("is_repost") or 0):
                role_bits.append("<div><span class=\"pill\">Likely repost</span></div>")
            quality_score = int(job.get("employer_quality_score") or 0)
            rows.append(
                "<tr>"
                f"<td><input type=\"checkbox\" name=\"job_key\" value=\"{escape(job['key'])}\"></td>"
                f"<td>{''.join(role_bits)}</td>"
                f"<td>{escape(job['location'])}</td>"
                f"<td><span class=\"score {label}\">{job['score']}</span></td>"
                f"<td>{grade}</td>"
                f"<td class=\"{label}\">{label.upper()}</td>"
                f"<td>{quality_score}</td>"
                f"<td>{_structured_signal_pills(job)}</td>"
                f"<td>{pipeline_status}</td>"
                f"<td>{escape(job['source'])}</td>"
                "</tr>"
            )
        rows_html = "".join(rows) if rows else "<tr><td colspan=\"10\">No jobs match the current filters.</td></tr>"
        queue_options = "".join(
            f"<option value=\"{name}\"{' selected' if queue == name else ''}>{label}</option>"
            for name, label in (("active", "Active"), ("actionable", "Actionable"), ("all", "All"))
        )
        status_options = "".join(
            f"<option value=\"{name}\"{' selected' if status == name else ''}>{label}</option>"
            for name, label in (("all", "All statuses"),) + tuple((item, item.replace('_', ' ').title()) for item in PIPELINE_STATUSES)
        )
        source_options = "".join(
            f"<option value=\"{escape(name)}\"{' selected' if source == name else ''}>{escape(name.title())}</option>"
            for name in ["all", *source_names]
        )
        day_options = "".join(
            f"<option value=\"{value}\"{' selected' if days_raw == value else ''}>{label}</option>"
            for value, label in (
                ("1", "24 hours (posted/seen)"),
                ("seen_1", "24 hours (first seen)"),
                ("3", "3 days"),
                ("7", "7 days"),
                ("14", "14 days"),
                ("all", "All"),
            )
        )
        sort_options = "".join(
            f"<option value=\"{name}\"{' selected' if sort_by == name else ''}>{label}</option>"
            for name, label in (
                ("newest", "Newest"),
                ("oldest", "Oldest"),
                ("score", "Score"),
                ("rating", "Rating"),
                ("source", "Source"),
            )
        )
        scan = _scan_snapshot()
        scan_mode = escape(str(scan.get("mode") or ""))
        scan_message = escape(str(scan.get("message") or ""))
        scan_started = escape(str(scan.get("started_at") or ""))
        scan_finished = escape(str(scan.get("finished_at") or ""))
        scan_state_label = "Running" if scan.get("running") else "Idle"
        board_total = _board_inventory_total(cfg)
        try:
            boards_cursor = int(db.get_cursor("boards_main") or 0)
        except Exception:
            boards_cursor = 0
        db_meta = dict(active_dashboard_db_meta)
        db_source = escape(str(db_meta.get("source") or "local"))
        db_strategy = escape(str(db_meta.get("strategy") or "configured"))
        db_path_label = escape(str(db_meta.get("path") or ""))
        db_last_seen = escape(str(db_meta.get("last_seen") or "unknown"))
        db_recent_jobs = int(db_meta.get("recent_jobs_24h") or 0)
        db_recent_first_seen_jobs = int(db_meta.get("recent_first_seen_24h") or 0)
        db_total_jobs = int(db_meta.get("total_jobs") or 0)
        shown_yes = sum(1 for job in jobs if (job.get("label") or "").strip().lower() == "yes")
        shown_maybe = sum(1 for job in jobs if (job.get("label") or "").strip().lower() == "maybe")
        shown_no = sum(1 for job in jobs if (job.get("label") or "").strip().lower() == "no")
        active_sources = len({(job.get("source") or "").strip().lower() for job in jobs if (job.get("source") or "").strip()})
        hidden_notes: list[str] = []
        if hidden_workday_keys:
            hidden_notes.append(f"{len(hidden_workday_keys)} legacy Workday duplicate(s)")
        if hidden_non_us_count:
            hidden_notes.append(f"{hidden_non_us_count} non-US job(s)")
        hidden_workday_note = (
            f"<p class=\"muted\">Hidden automatically: {', '.join(hidden_notes)}.</p>"
            if hidden_notes
            else ""
        )
        page_message_html = f"<p><strong>{escape(page_message)}</strong></p>" if page_message else ""
        rescore_options = "".join(
            f"<option value=\"{value}\"{' selected' if rescore_limit_raw == value else ''}>{label}</option>"
            for value, label in (("100", "100 jobs"), ("250", "250 jobs"), ("500", "500 jobs"), ("1000", "1000 jobs"), ("all", "All jobs"))
        )
        body = (
            "<div class=\"card\">"
            "<div class=\"hero\">"
            "<div>"
            "<h1>Job Review Board</h1>"
            "<p class=\"muted\">Review fresh roles, update your pipeline quickly, and run scans without leaving the page.</p>"
            f"<p class=\"muted\">Dashboard data source: <strong>{db_source}</strong> | strategy: <strong>{db_strategy}</strong></p>"
            f"<p class=\"muted\">Active DB: <code>{db_path_label}</code></p>"
            f"<p class=\"muted\">DB freshness: last seen <strong>{db_last_seen}</strong> | recent jobs by posted/first-seen (24h): <strong>{db_recent_jobs}</strong> | recent jobs by first-seen only (24h): <strong>{db_recent_first_seen_jobs}</strong> | total stored: <strong>{db_total_jobs}</strong></p>"
            "<ul class=\"helper-list\">"
            "<li><strong>Scan Main Sources</strong> is the fastest local refresh for direct high-priority companies.</li>"
            "<li><strong>Scan Next Board Batch</strong> is the recommended local board action when you just want a quick incremental update.</li>"
            "<li><strong>Scan Full Board Sweep</strong> is intentionally gated because it can take a while and is usually better left to GitHub Actions.</li>"
            "</ul>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Scanner Status</h2>"
            f"<p class=\"muted\">State: <strong>{scan_state_label}</strong>"
            + (f" | Mode: <strong>{scan_mode}</strong>" if scan_mode else "")
            + "</p>"
            f"<p class=\"muted\">{scan_message}</p>"
            + (f"<p class=\"muted\">Started: {scan_started}</p>" if scan_started else "")
            + (f"<p class=\"muted\">Finished: {scan_finished}</p>" if scan_finished else "")
            + f"<p class=\"muted\">Current board cursor: <strong>{boards_cursor}</strong> / {board_total or 'unknown'}</p>"
            + "</div>"
            "</div>"
            "<div class=\"stats\">"
            f"<div class=\"stat\"><strong>{len(jobs)}</strong><span>Jobs shown</span></div>"
            f"<div class=\"stat\"><strong>{shown_yes}</strong><span>Yes matches</span></div>"
            f"<div class=\"stat\"><strong>{shown_maybe}</strong><span>Maybe matches</span></div>"
            f"<div class=\"stat\"><strong>{shown_no}</strong><span>No matches</span></div>"
            "</div>"
            f"<p class=\"muted\">Showing {len(jobs)} jobs from {len(all_jobs)} stored roles across {active_sources} source(s) in the current view.</p>"
            f"{page_message_html}"
            f"{hidden_workday_note}"
            "<div class=\"card\">"
            "<h2>Run Scanner</h2>"
            "<form method=\"post\" action=\"/scan\" class=\"actions\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<input type=\"hidden\" name=\"rescore_limit\" value=\"{escape(rescore_limit_raw)}\">"
            "<div class=\"scan-grid\">"
            "<div class=\"scan-card\">"
            "<h3>Scan Main Sources</h3>"
            "<p>Fastest local refresh for core companies and direct sources.</p>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"main\">Run Main Sources</button></div>"
            "</div>"
            "<div class=\"scan-card\">"
            "<h3>Scan Next Board Batch</h3>"
            "<p>Recommended local board refresh. Advances the saved cursor without doing a full wrap.</p>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"boards\">Run Next Board Batch</button></div>"
            "</div>"
            "<div class=\"scan-card danger-card\">"
            "<h3>Scan Full Board Sweep</h3>"
            "<p>Slowest option. Walks the cursor until every board is covered, so use it only when you explicitly want a full local pass.</p>"
            "<label class=\"confirm-line\"><input type=\"checkbox\" name=\"confirm_full_sweep\" value=\"1\">I really want a full local sweep</label>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"all\" class=\"danger-button\">Run Full Board Sweep</button></div>"
            "</div>"
            "</div>"
            "</form>"
            "<p class=\"muted\">For day-to-day use, pull the latest repo data, open the web UI, and use either Main Sources or Next Board Batch. Full sweeps are better as occasional manual actions.</p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Filter Jobs</h2>"
            "<form method=\"get\" action=\"/\" class=\"actions\" data-autosubmit=\"true\">"
            f"<label>Queue<br><select name=\"queue\">{queue_options}</select></label>"
            f"<label>Freshness<br><select name=\"days\">{day_options}</select></label>"
            f"<label>Source<br><select name=\"source\">{source_options}</select></label>"
            f"<label>Status<br><select name=\"status\">{status_options}</select></label>"
            f"<label>Sort<br><select name=\"sort\">{sort_options}</select></label>"
            "<button type=\"submit\">Apply Filters</button>"
            "</form>"
            "<form method=\"post\" action=\"/jobs/re-evaluate\" class=\"actions\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<label>Re-score Batch<br><select name=\"rescore_limit\">{rescore_options}</select></label>"
            "<button type=\"submit\">Batch Re-score Jobs</button>"
            "</form>"
            "</div>"
            "</div>"
            "<div class=\"card\">"
            "<form method=\"post\" action=\"/jobs/bulk-update\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<input type=\"hidden\" name=\"rescore_limit\" value=\"{escape(rescore_limit_raw)}\">"
            "<div class=\"actions\">"
            "<label>Bulk Status<br><select name=\"bulk_status\">"
            + "".join(f"<option value=\"{escape(item)}\">{escape(item.replace('_', ' ').title())}</option>" for item in PIPELINE_STATUSES)
            + "</select></label>"
            "<button type=\"submit\">Update Selected Jobs</button>"
            "</div>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Select</th><th>Role</th><th>Location</th><th>Score</th><th>Grade</th><th>Label</th><th>Employer</th><th>Signals</th><th>Status</th><th>Source</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
            "</div>"
            "</form>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Jobs", body)]

    def _job_page(start_response, key: str):
        all_jobs = db.list_jobs_for_board(limit=4000)
        hidden_workday_keys = _workday_legacy_duplicate_keys(all_jobs)
        if key in hidden_workday_keys:
            canonical_key = _canonical_workday_key_for_hidden(key, all_jobs)
            if canonical_key:
                return _redirect(start_response, f"/job?key={quote(canonical_key, safe='')}")
        job = db.get_job(key)
        if job is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Job not found."]

        job, evaluation = _persist_evaluation(job)

        flags = _flags()
        resumes = db.list_generated_resumes(job["key"])
        skill_pills = "".join(f"<span class=\"pill\">{escape(skill)}</span>" for skill in (evaluation.matched_strong + evaluation.matched_moderate)[:16])
        reasons = "".join(f"<li>{escape(reason)}</li>" for reason in evaluation.reasons)
        dimensions_html = "".join(
            "<tr>"
            f"<td>{escape(dim.name.replace('_', ' ').title())}</td>"
            f"<td>{int(dim.weight * 100)}%</td>"
            f"<td>{dim.score}</td>"
            f"<td>{round(dim.weighted_points, 1)}</td>"
            f"<td>{escape(dim.reason)}</td>"
            "</tr>"
            for dim in evaluation.dimensions
        )
        resume_block = (
            "".join(
                f"<li><a href=\"{_packet_link(item)}\">Prompt packet #{item['id']}</a> <span class=\"muted\">{escape(item['created_at'])}</span></li>"
                for item in resumes
            ) or "<li>No generated prompt packet yet.</li>"
        )
        skill_match_html = skill_pills or "<span class=\"muted\">No overlapping skills found from the stored text.</span>"
        structured = _job_structured(job)
        structured_rows = []
        for field, label in (
            ("remote_mode", "Remote Mode"),
            ("salary_min", "Salary Min"),
            ("salary_max", "Salary Max"),
            ("salary_currency", "Salary Currency"),
            ("salary_period", "Salary Period"),
            ("years_experience_min", "Experience Min"),
            ("years_experience_max", "Experience Max"),
            ("visa_sponsorship", "Visa Sponsorship"),
            ("security_clearance", "Security Clearance"),
            ("citizenship_requirement", "Citizenship Requirement"),
            ("employment_type", "Employment Type"),
            ("resume_match_score", "Resume/JD Match"),
            ("resume_match_cap", "Resume/JD Cap"),
            ("resume_match_cap_reason", "Resume/JD Cap Reason"),
            ("company_priority_delta", "Company Priority Delta"),
            ("company_priority_reason", "Company Priority Reason"),
            ("label_threshold_yes", "Yes Threshold"),
            ("label_threshold_maybe", "Maybe Threshold"),
            ("feedback_score_delta", "Feedback Score Delta"),
            ("feedback_reasons", "Feedback Reasons"),
        ):
            if field not in structured:
                continue
            value = structured[field]
            if isinstance(value, bool):
                display = "Yes" if value else "No"
            elif isinstance(value, list):
                display = ", ".join(str(item) for item in value) if value else ""
            elif isinstance(value, int) and field.startswith("salary_"):
                display = f"${value:,}"
            else:
                display = str(value)
            structured_rows.append(f"<tr><td>{escape(label)}</td><td>{escape(display)}</td></tr>")
        structured_rows_html = "".join(structured_rows) or "<tr><td colspan=\"2\">No structured fields extracted yet.</td></tr>"
        repost_html = (
            f"<span class=\"pill\">Likely repost of <a href=\"/job?key={quote(job.get('repost_of_key') or '', safe='')}\">{escape(job.get('repost_of_key') or '')}</a></span>"
            if int(job.get("is_repost") or 0) and (job.get("repost_of_key") or "")
            else "<span class=\"pill\">Not marked as a repost</span>"
        )

        generate_form = ""
        if flags.get("resume_generation", True):
            generate_form = (
                "<form method=\"post\" action=\"/job/generate-resume\">"
                f"<input type=\"hidden\" name=\"key\" value=\"{escape(job['key'])}\">"
                "<button type=\"submit\">Generate Prompt Packet</button>"
                "</form>"
            )
        resume_action_html = generate_form or "<p class=\"muted\">Resume generation is disabled in the feature switchboard.</p>"
        current_status = job.get("pipeline_status") or "new"
        status_options = "".join(
            f"<option value=\"{escape(status)}\"{' selected' if status == current_status else ''}>{escape(status.replace('_', ' ').title())}</option>"
            for status in PIPELINE_STATUSES
        )

        body = (
            "<div class=\"grid\">"
            "<div>"
            "<div class=\"card\">"
            f"<h1>{escape(job['title'])}</h1>"
            f"<p class=\"muted\">{escape(job['company'])} | {escape(job['location'])} | {escape(job['source'])}</p>"
            f"<p><span class=\"score {escape(job['label'])}\">Score {job['score']}</span> | Grade <strong>{escape(job.get('grade') or evaluation.grade)}</strong> | <strong>{escape(job['label']).upper()}</strong></p>"
            f"<p><span class=\"pill\">Employer Quality {int(job.get('employer_quality_score') or 0)}</span> {repost_html}</p>"
            f"<p>{escape(job.get('fit_summary') or evaluation.fit_summary)}</p>"
            f"<ul>{reasons}</ul>"
            f"<p><a href=\"{escape(job['url'])}\" target=\"_blank\" rel=\"noreferrer\">Open original posting</a></p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Weighted Evaluation</h2>"
            "<table><thead><tr><th>Dimension</th><th>Weight</th><th>Score</th><th>Points</th><th>Reason</th></tr></thead>"
            f"<tbody>{dimensions_html}</tbody></table>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Stored Job Description</h2>"
            f"<pre>{escape(job.get('description') or 'No job description stored yet for this job.')}</pre>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Structured Extraction</h2>"
            "<table><thead><tr><th>Field</th><th>Value</th></tr></thead>"
            f"<tbody>{structured_rows_html}</tbody></table>"
            "</div>"
            "</div>"
            "<div>"
            "<div class=\"card\">"
            "<h2>Skill Match</h2>"
            f"<div>{skill_match_html}</div>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Inventory Signals</h2>"
            f"<p>{_structured_signal_pills(job)}</p>"
            f"<p class=\"muted\">{escape(job.get('employer_quality_reason') or '')}</p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Prompt Packet Actions</h2>"
            f"{resume_action_html}"
            "<h3>Generated Prompt Packets</h3>"
            f"<ul>{resume_block}</ul>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Pipeline Tracker</h2>"
            "<form method=\"post\" action=\"/job/pipeline\">"
            f"<input type=\"hidden\" name=\"key\" value=\"{escape(job['key'])}\">"
            f"<label>Status<br><select name=\"pipeline_status\">{status_options}</select></label>"
            f"<label>Follow Up Date<br><input type=\"text\" name=\"follow_up_date\" value=\"{escape(job.get('follow_up_date') or '')}\" placeholder=\"YYYY-MM-DD\"></label>"
            f"<label>Notes<br><textarea name=\"pipeline_notes\">{escape(job.get('pipeline_notes') or '')}</textarea></label>"
            "<div class=\"actions\"><button type=\"submit\">Save Pipeline Status</button></div>"
            "</form>"
            "<p class=\"muted\">Statuses like Applied, Shortlisted, Interview, Rejected, and Archived also feed the lightweight feedback reranker.</p>"
            f"<p class=\"muted\">Last updated: {escape(job.get('pipeline_updated_at') or 'never')}</p>"
            "</div>"
            "</div>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout(job["title"], body)]

    def _manual_page(start_response, values: dict[str, str] | None = None, message: str = ""):
        flags = _flags()
        if not flags.get("manual_jd", True):
            body = "<div class=\"card\"><h1>Paste JD</h1><p class=\"muted\">Manual JD scoring is disabled in the feature switchboard.</p></div>"
            start_response("200 OK", _html_headers())
            return [_layout("Paste JD", body)]

        values = values or {}
        body = (
            "<div class=\"card\">"
            "<h1>Paste an External Job Description</h1>"
            "<p class=\"muted\">Use this for roles that did not come from the scanner. The job is scored, saved, and can then generate a resume draft.</p>"
            f"<p>{escape(message)}</p>"
            "<form method=\"post\" action=\"/manual-jd\">"
            "<div class=\"split\">"
            f"<label>Company<br><input type=\"text\" name=\"company\" value=\"{escape(values.get('company', ''))}\"></label>"
            f"<label>Job Title<br><input type=\"text\" name=\"title\" value=\"{escape(values.get('title', ''))}\" required></label>"
            "</div>"
            "<div class=\"split\">"
            f"<label>Location<br><input type=\"text\" name=\"location\" value=\"{escape(values.get('location', ''))}\"></label>"
            f"<label>Job URL<br><input type=\"url\" name=\"url\" value=\"{escape(values.get('url', ''))}\" placeholder=\"https://...\"></label>"
            "</div>"
            f"<label>Job Description<br><textarea name=\"description\" required>{escape(values.get('description', ''))}</textarea></label>"
            "<div class=\"actions\"><button type=\"submit\">Score and Save Job</button></div>"
            "</form>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Paste JD", body)]

    def _settings_page(start_response):
        flags = _flags()
        items = []
        for name, enabled in flags.items():
            checked = " checked" if enabled else ""
            label = name.replace("_", " ").title()
            items.append(
                f"<label><input type=\"checkbox\" name=\"{escape(name)}\" value=\"1\"{checked}> {escape(label)}</label>"
            )
        body = (
            "<div class=\"card\">"
            "<h1>Feature Switchboard</h1>"
            "<p class=\"muted\">This is the single toggle panel for the useful features you asked for. Scanner modes stay intact; these switches decide which layers stay active.</p>"
            "<form method=\"post\" action=\"/settings\">"
            "<div class=\"split\">"
            f"{''.join(f'<div>{item}</div>' for item in items)}"
            "</div>"
            "<div class=\"actions\"><button type=\"submit\">Save Feature Settings</button></div>"
            "</form>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Feature Switchboard", body)]

    def _boards_page(start_response, query: dict[str, list[str]]):
        status = ((query.get("status") or ["all"])[-1] or "all").strip().lower()
        platform = ((query.get("platform") or ["all"])[-1] or "all").strip().lower()
        stats = db.get_board_stats()
        boards = db.list_boards(
            limit=2000,
            status="" if status == "all" else status,
            platform="" if platform == "all" else platform,
        )
        all_boards = db.list_boards(limit=5000)
        platforms = sorted({(board.get("platform") or "").strip().lower() for board in all_boards if (board.get("platform") or "").strip()})
        platform_counts: dict[str, int] = {}
        for board in all_boards:
            name = (board.get("platform") or "").strip().lower()
            if not name:
                continue
            platform_counts[name] = platform_counts.get(name, 0) + 1
        rows = []
        for board in boards:
            url = escape(board.get("url") or "")
            rows.append(
                "<tr>"
                f"<td>{escape(board.get('platform') or '')}</td>"
                f"<td>{escape(board.get('company') or '')}</td>"
                f"<td>{escape(board.get('status') or '')}</td>"
                f"<td>{int(board.get('job_count') or 0)}</td>"
                f"<td>{escape(board.get('last_checked') or '')}</td>"
                f"<td>{int(board.get('fail_count') or 0)}</td>"
                f"<td>{escape(board.get('fail_reason') or '')}</td>"
                f"<td><a href=\"{url}\" target=\"_blank\" rel=\"noreferrer\">Open board</a></td>"
                "</tr>"
            )
        rows_html = "".join(rows) if rows else "<tr><td colspan=\"8\">No boards match the current filters.</td></tr>"
        status_options = "".join(
            f"<option value=\"{name}\"{' selected' if status == name else ''}>{label}</option>"
            for name, label in (("all", "All statuses"), ("active", "Active"), ("degraded", "Degraded"), ("dead", "Dead"))
        )
        platform_options = "".join(
            f"<option value=\"{escape(name)}\"{' selected' if platform == name else ''}>{escape(name.title())}</option>"
            for name in ["all", *platforms]
        )
        platform_pills = "".join(
            f"<span class=\"pill\">{escape(name.title())}: {count}</span>"
            for name, count in sorted(platform_counts.items())
        ) or "<span class=\"muted\">No boards recorded yet.</span>"
        body = (
            "<div class=\"card\">"
            "<h1>Board Health</h1>"
            "<p class=\"muted\">Track which ATS boards are healthy, which ones are degrading, and which ones are effectively dead before they waste scan time.</p>"
            "<div class=\"stats\">"
            f"<div class=\"stat\"><strong>{stats['total']}</strong><span>Total boards</span></div>"
            f"<div class=\"stat\"><strong>{stats['active']}</strong><span>Active</span></div>"
            f"<div class=\"stat\"><strong>{stats['degraded']}</strong><span>Degraded</span></div>"
            f"<div class=\"stat\"><strong>{stats['dead']}</strong><span>Dead</span></div>"
            "</div>"
            f"<div>{platform_pills}</div>"
            "<form method=\"get\" action=\"/boards\" class=\"actions\" data-autosubmit=\"true\">"
            f"<label>Status<br><select name=\"status\">{status_options}</select></label>"
            f"<label>Platform<br><select name=\"platform\">{platform_options}</select></label>"
            "<button type=\"submit\">Apply Filters</button>"
            "</form>"
            "</div>"
            "<div class=\"card\">"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Platform</th><th>Company</th><th>Status</th><th>Jobs</th><th>Last Checked</th><th>Failures</th><th>Reason</th><th>URL</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
            "</div>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Board Health", body)]

    def _health_page(start_response, query: dict[str, list[str]]):
        entity_type = ((query.get("type") or ["all"])[-1] or "all").strip().lower()
        mode = ((query.get("mode") or ["all"])[-1] or "all").strip().lower()
        status = ((query.get("status") or ["all"])[-1] or "all").strip().lower()
        health_rows = db.get_source_health(
            entity_type="" if entity_type == "all" else entity_type,
            mode="" if mode == "all" else mode,
            status="" if status == "all" else status,
            limit=1000,
        )
        run_rows = db.list_source_runs(
            limit=150,
            entity_type="" if entity_type == "all" else entity_type,
            mode="" if mode == "all" else mode,
            status="" if status in {"all", "healthy", "degraded", "broken"} else status,
        )
        summary = db.get_health_summary()
        type_options = "".join(
            f"<option value=\"{name}\"{' selected' if entity_type == name else ''}>{label}</option>"
            for name, label in (("all", "All"), ("main", "Main Sources"), ("board", "Boards"))
        )
        mode_options = "".join(
            f"<option value=\"{name}\"{' selected' if mode == name else ''}>{label}</option>"
            for name, label in (("all", "All modes"), ("main", "Main"), ("boards", "Boards"))
        )
        status_options = "".join(
            f"<option value=\"{name}\"{' selected' if status == name else ''}>{label}</option>"
            for name, label in (
                ("all", "All states"),
                ("healthy", "Healthy"),
                ("degraded", "Degraded"),
                ("broken", "Broken"),
                ("success", "Last run success"),
                ("empty", "Last run empty"),
                ("error", "Last run error"),
            )
        )
        health_table = []
        for row in health_rows:
            health_table.append(
                "<tr>"
                f"<td>{escape(row['source_key'])}</td>"
                f"<td>{escape(row['entity_type'])}</td>"
                f"<td>{escape(row.get('platform') or '')}</td>"
                f"<td>{escape(row.get('company') or '')}</td>"
                f"<td>{escape(row['health'])}</td>"
                f"<td>{escape(row['latest_status'])}</td>"
                f"<td>{row['latest_fetched_count']}</td>"
                f"<td>{row['latest_matched_count']}</td>"
                f"<td>{row['latest_new_count']}</td>"
                f"<td>{int(round(row['latest_jd_coverage']))}%</td>"
                f"<td>{row['failure_streak']}</td>"
                f"<td>{escape(row.get('last_success_at') or '')}</td>"
                f"<td>{escape(row.get('last_error') or '')}</td>"
                "</tr>"
            )
        health_rows_html = "".join(health_table) if health_table else "<tr><td colspan=\"13\">No health rows match the current filters.</td></tr>"
        run_table = []
        for row in run_rows:
            run_table.append(
                "<tr>"
                f"<td>{escape(row['finished_at'])}</td>"
                f"<td>{escape(row['source_key'])}</td>"
                f"<td>{escape(row['entity_type'])}</td>"
                f"<td>{escape(row['status'])}</td>"
                f"<td>{int(row['fetched_count'])}</td>"
                f"<td>{int(row['matched_count'])}</td>"
                f"<td>{int(row['new_count'])}</td>"
                f"<td>{int(row['latency_ms'])}</td>"
                f"<td>{int(round(float(row['jd_coverage'] or 0.0)))}%</td>"
                f"<td>{escape(row.get('error_text') or '')}</td>"
                "</tr>"
            )
        run_rows_html = "".join(run_table) if run_table else "<tr><td colspan=\"10\">No run history yet.</td></tr>"
        body = (
            "<div class=\"card\">"
            "<h1>Source Health</h1>"
            "<p class=\"muted\">Track both main sources and board runs to spot quality regressions, empty returns, and failing adapters quickly.</p>"
            "<div class=\"stats\">"
            f"<div class=\"stat\"><strong>{summary['total']}</strong><span>Total tracked</span></div>"
            f"<div class=\"stat\"><strong>{summary['healthy']}</strong><span>Healthy</span></div>"
            f"<div class=\"stat\"><strong>{summary['degraded']}</strong><span>Degraded</span></div>"
            f"<div class=\"stat\"><strong>{summary['broken']}</strong><span>Broken</span></div>"
            "</div>"
            f"<p class=\"muted\">Recent failures in the last 24 hours: <strong>{summary['failures_24h']}</strong></p>"
            "<form method=\"get\" action=\"/health\" class=\"actions\" data-autosubmit=\"true\">"
            f"<label>Type<br><select name=\"type\">{type_options}</select></label>"
            f"<label>Mode<br><select name=\"mode\">{mode_options}</select></label>"
            f"<label>Status<br><select name=\"status\">{status_options}</select></label>"
            "<button type=\"submit\">Apply Filters</button>"
            "</form>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Latest Health By Source</h2>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Key</th><th>Type</th><th>Platform</th><th>Company</th><th>Health</th><th>Last Status</th><th>Fetched</th><th>Matched</th><th>New</th><th>JD</th><th>Streak</th><th>Last Success</th><th>Last Error</th></tr></thead>"
            f"<tbody>{health_rows_html}</tbody></table>"
            "</div>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Recent Run History</h2>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Finished</th><th>Key</th><th>Type</th><th>Status</th><th>Fetched</th><th>Matched</th><th>New</th><th>Latency ms</th><th>JD</th><th>Error</th></tr></thead>"
            f"<tbody>{run_rows_html}</tbody></table>"
            "</div>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Health", body)]

    def _resume_page(start_response, resume_id: int):
        resume = db.get_generated_resume(resume_id)
        if resume is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Resume packet not found."]
        if (resume.get("format") or "").strip().lower() == "prompt_packet":
            return _redirect(start_response, f"/packet?id={resume_id}")
        body = (
            "<div class=\"card\">"
            f"<h1>Resume Packet #{resume['id']}</h1>"
            f"<p class=\"muted\">{escape(resume['job_title'])} | {escape(resume['company'])} | {escape(resume['created_at'])}</p>"
            f"<pre>{escape(resume['content'])}</pre>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Resume Packet", body)]

    def _packet_page(start_response, resume_id: int):
        resume = db.get_generated_resume(resume_id)
        if resume is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Prompt packet not found."]
        artifacts = db.list_generated_artifacts(resume_id)
        artifact_cards: list[str] = []
        for artifact in artifacts:
            artifact_id = int(artifact["id"])
            pre_id = f"artifact-{artifact_id}"
            artifact_cards.append(
                "<div class=\"card\">"
                f"<h2>{escape(artifact.get('name') or 'Artifact')}</h2>"
                f"<div class=\"artifact-meta\"><span class=\"pill\">{escape(artifact.get('filename') or '')}</span><span class=\"pill\">{escape((artifact.get('format') or 'text').upper())}</span></div>"
                "<div class=\"artifact-actions\">"
                f"<button type=\"button\" onclick=\"copyText('{pre_id}')\">Copy</button>"
                f"<a class=\"button-link\" href=\"/artifact?id={artifact_id}\">Download</a>"
                "</div>"
                f"<pre id=\"{pre_id}\">{escape(artifact.get('content') or '')}</pre>"
                "</div>"
            )
        if not artifact_cards:
            artifact_cards.append(
                "<div class=\"card\"><p class=\"muted\">No separate artifacts were saved for this packet.</p>"
                f"<pre>{escape(resume.get('content') or '')}</pre></div>"
            )
        bundle_pre_id = f"packet-bundle-{resume_id}"
        body = (
            "<div class=\"card\">"
            f"<h1>Prompt Packet #{resume['id']}</h1>"
            f"<p class=\"muted\">{escape(resume['job_title'])} | {escape(resume['company'])} | {escape(resume['created_at'])}</p>"
            "<p class=\"muted\">Use the separate files below for prompt, JD markdown, generated resume TeX, and cover letter.</p>"
            "<div class=\"artifact-actions\">"
            f"<button type=\"button\" onclick=\"copyText('{bundle_pre_id}')\">Copy Full Packet</button>"
            "</div>"
            f"<pre id=\"{bundle_pre_id}\">{escape(resume['content'])}</pre>"
            "</div>"
            + "".join(artifact_cards)
        )
        start_response("200 OK", _html_headers())
        return [_layout("Prompt Packet", body)]

    def _artifact_download(start_response, artifact_id: int):
        artifact = db.get_generated_artifact(artifact_id)
        if artifact is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Artifact not found."]
        filename = (artifact.get("filename") or f"artifact-{artifact_id}.txt").replace("\"", "")
        start_response(
            "200 OK",
            [
                ("Content-Type", _artifact_content_type(str(artifact.get("format") or ""))),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
                ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            ],
        )
        return [str(artifact.get("content") or "").encode("utf-8")]

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/") or "/"
        method = environ.get("REQUEST_METHOD", "GET").upper()
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        if method == "GET":
            _replace_dashboard_db()
            _maybe_pull_repo()

        def _redirect_home(form: dict[str, str | list[str]] | None = None, *, message: str = ""):
            form = form or {}
            days = form.get("days", str(RECENT_JOB_DAYS))
            queue = form.get("queue", "active")
            source = form.get("source", "all")
            status = form.get("status", "all")
            sort_by = form.get("sort", "newest")
            rescore_limit = form.get("rescore_limit", "500")
            query = (
                f"/?days={quote(str(days), safe='')}"
                f"&queue={quote(str(queue), safe='')}"
                f"&source={quote(str(source), safe='')}"
                f"&status={quote(str(status), safe='')}"
                f"&sort={quote(str(sort_by), safe='')}"
                f"&rescore_limit={quote(str(rescore_limit), safe='')}"
            )
            if message:
                query += f"&message={quote(message, safe='')}"
            return _redirect(
                start_response,
                query,
            )

        if path == "/" and method == "GET":
            return _jobs_page(start_response, query)

        if path == "/job" and method == "GET":
            key = (query.get("key") or [""])[-1]
            return _job_page(start_response, key)

        if path == "/jobs/re-evaluate" and method == "POST":
            form = _read_post(environ)
            rescore_limit_raw = form.get("rescore_limit", "500") or "500"
            if rescore_limit_raw == "all":
                rescore_limit = None
            else:
                try:
                    rescore_limit = max(int(rescore_limit_raw), 1)
                except ValueError:
                    rescore_limit = 500
            jobs = _filtered_jobs_snapshot(form, limit=rescore_limit)
            processed = _batch_refresh(jobs)
            return _redirect_home(form, message=f"Re-scored {processed} job(s) from the current filtered view.")

        if path == "/scan" and method == "POST":
            form = _read_post(environ)
            mode = (form.get("scan_mode", "all") or "all").strip().lower()
            if mode not in {"main", "boards", "all"}:
                mode = "all"
            if mode == "all" and form.get("confirm_full_sweep") != "1":
                return _redirect_home(
                    form,
                    message="Full board sweep not started. Check the confirmation box if you really want a full local sweep.",
                )
            started = _start_scan(mode)
            if not started:
                current = _scan_snapshot()
                _set_scan_state(
                    message=f"A scan is already running ({current.get('mode') or 'unknown'}). Wait for it to finish before starting another one."
                )
            return _redirect_home(form)

        if path == "/jobs/bulk-update" and method == "POST":
            try:
                size = int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError:
                size = 0
            raw = environ["wsgi.input"].read(size).decode("utf-8") if size > 0 else ""
            parsed = parse_qs(raw, keep_blank_values=True)
            keys = parsed.get("job_key") or []
            bulk_status = ((parsed.get("bulk_status") or ["new"])[-1] or "new").strip()
            for key in keys:
                db.update_pipeline(key=key, pipeline_status=bulk_status)
            return _redirect_home(
                {
                    "days": (parsed.get("days") or [str(RECENT_JOB_DAYS)])[-1],
                    "queue": (parsed.get("queue") or ["active"])[-1],
                    "source": (parsed.get("source") or ["all"])[-1],
                    "status": (parsed.get("status") or ["all"])[-1],
                    "sort": (parsed.get("sort") or ["newest"])[-1],
                    "rescore_limit": (parsed.get("rescore_limit") or ["500"])[-1],
                }
            )

        if path == "/job/generate-resume" and method == "POST":
            form = _read_post(environ)
            key = form.get("key", "")
            job = db.get_job(key)
            if job is None:
                start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
                return [b"Job not found."]
            flags = _flags()
            if not flags.get("resume_generation", True):
                return _redirect(start_response, f"/job?key={quote(key, safe='')}")
            thresholds = _label_thresholds()
            evaluation = evaluate_job(
                job["title"],
                job.get("description", ""),
                company=job["company"],
                location=job["location"],
                source=job.get("source", ""),
                require_us_location=cfg.filter.require_us_location,
                yes_threshold=thresholds.yes,
                maybe_threshold=thresholds.maybe,
            )
            packet = generate_resume_packet(job, evaluation)
            resume_id = db.save_generated_resume(
                job_key=job["key"],
                job_title=job["title"],
                company=job["company"],
                content=str(packet["bundle_markdown"]),
                format="prompt_packet",
            )
            for artifact in packet.get("artifacts", []):
                if not isinstance(artifact, dict):
                    continue
                db.save_generated_artifact(
                    resume_id=resume_id,
                    name=str(artifact.get("name") or "Artifact"),
                    filename=str(artifact.get("filename") or ""),
                    format=str(artifact.get("format") or "text"),
                    content=str(artifact.get("content") or ""),
                )
            return _redirect(start_response, f"/packet?id={resume_id}")

        if path == "/job/pipeline" and method == "POST":
            form = _read_post(environ)
            key = form.get("key", "")
            db.update_pipeline(
                key=key,
                pipeline_status=form.get("pipeline_status", "new").strip() or "new",
                pipeline_notes=form.get("pipeline_notes", "").strip(),
                follow_up_date=form.get("follow_up_date", "").strip(),
            )
            return _redirect(start_response, f"/job?key={quote(key, safe='')}")

        if path == "/manual-jd" and method == "GET":
            return _manual_page(start_response)

        if path == "/manual-jd" and method == "POST":
            form = _read_post(environ)
            title = form.get("title", "").strip()
            description = form.get("description", "").strip()
            if not title or not description:
                return _manual_page(start_response, form, "Job title and description are required.")
            company = form.get("company", "").strip() or "Manual Entry"
            location = form.get("location", "").strip() or "Unknown Location"
            url = form.get("url", "").strip() or f"manual://{_slug(company)}"
            thresholds = _label_thresholds()
            evaluation = evaluate_job(
                title,
                description,
                company=company,
                location=location,
                source="manual",
                require_us_location=cfg.filter.require_us_location,
                yes_threshold=thresholds.yes,
                maybe_threshold=thresholds.maybe,
            )
            digest = hashlib.sha1(f"{company.lower()}|{title.lower()}|{description[:240]}".encode("utf-8")).hexdigest()[:12]
            manual_key = f"manual:{_slug(company)}:{_slug(title)}:{digest}"
            db.create_manual_job(
                key=manual_key,
                company=company,
                title=title,
                location=location,
                url=url,
                description=description,
                score=evaluation.score,
                label=evaluation.label,
                grade=evaluation.grade,
                evaluation_json=evaluation.to_json(),
                fit_summary=evaluation.fit_summary,
            )
            return _redirect(start_response, f"/job?key={quote(manual_key, safe='')}")

        if path == "/settings" and method == "GET":
            return _settings_page(start_response)

        if path == "/boards" and method == "GET":
            return _boards_page(start_response, query)

        if path == "/health" and method == "GET":
            return _health_page(start_response, query)

        if path == "/settings" and method == "POST":
            form = _read_post(environ)
            for name in defaults:
                db.set_feature_flag(name, form.get(name) == "1")
            return _redirect(start_response, "/settings")

        if path == "/resume" and method == "GET":
            raw_id = (query.get("id") or ["0"])[-1]
            try:
                resume_id = int(raw_id)
            except ValueError:
                resume_id = 0
            return _resume_page(start_response, resume_id)

        if path == "/packet" and method == "GET":
            raw_id = (query.get("id") or ["0"])[-1]
            try:
                resume_id = int(raw_id)
            except ValueError:
                resume_id = 0
            return _packet_page(start_response, resume_id)

        if path == "/artifact" and method == "GET":
            raw_id = (query.get("id") or ["0"])[-1]
            try:
                artifact_id = int(raw_id)
            except ValueError:
                artifact_id = 0
            return _artifact_download(start_response, artifact_id)

        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found."]

    _replace_dashboard_db()

    with make_server(host, port, app) as httpd:
        print(f"Job Radar web UI running at http://{host}:{port}")
        httpd.serve_forever()
