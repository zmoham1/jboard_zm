"""Probe SmartRecruiters endpoints for boards that return zero postings.

299 of 323 SmartRecruiters boards have returned "0 jobs returned" on every
sweep since 2026-05-11, while 24 work. The URL shapes are identical, so the
difference is not slug parsing. This probes several endpoints for a sample of
failing and working slugs so the real cause is measured rather than guessed.

Run on CI, where outbound network is available.
"""
from __future__ import annotations

import json
import sys

import requests

FAILING = ["CoStarGroup", "Criteo", "AppFolio", "Compass", "doclerholding"]
WORKING = ["Visa", "Yardi", "Buildium"]

HEADERS = {"accept": "application/json", "user-agent": "Mozilla/5.0"}


def probe(label: str, url: str, params: dict | None = None) -> None:
    try:
        r = requests.get(url, params=params or {}, headers=HEADERS, timeout=30)
    except Exception as exc:
        print(f"      {label:<26} EXC {type(exc).__name__}: {exc}")
        return
    body = ""
    count = None
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            data = r.json()
            if isinstance(data, dict):
                count = data.get("totalFound")
                for key in ("content", "postings", "jobs"):
                    if isinstance(data.get(key), list):
                        count = f"{count} totalFound / {len(data[key])} in '{key}'"
                        break
                body = json.dumps(data)[:160]
        except Exception:
            body = r.text[:160]
    else:
        body = r.text[:160].replace("\n", " ")
    print(f"      {label:<26} HTTP {r.status_code}  count={count}")
    if r.status_code != 200 or count in (None, 0, "0 totalFound / 0 in 'content'"):
        print(f"        body: {body[:150]}")


def main() -> int:
    for group, slugs in (("FAILING", FAILING), ("WORKING", WORKING)):
        print(f"\n=== {group} SLUGS ===")
        for slug in slugs:
            print(f"  {slug}:")
            # 1. Endpoint the adapter uses today.
            probe("v1 postings", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                  {"offset": 0, "limit": 100})
            # 2. Same endpoint without paging params, in case params break it.
            probe("v1 postings (no params)", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
            # 3. Public careers-page JSON used by the widget.
            probe("careers api", f"https://careers.smartrecruiters.com/{slug}/api/groups")
            # 4. The public HTML page — proves whether the company exists at all.
            probe("public html", f"https://jobs.smartrecruiters.com/{slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
