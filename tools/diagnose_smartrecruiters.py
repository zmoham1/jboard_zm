"""Find which endpoint the SmartRecruiters careers page actually calls.

Probe 1 established that api.smartrecruiters.com/v1/companies/<slug>/postings
returns {"totalFound": 0, "content": []} for 299 boards whose careers pages
load fine, and under-reports badly even for the 24 that "work" (Visa returns
2 postings). So the adapter is on the wrong endpoint.

This fetches the careers page, extracts any API URLs it references, counts the
job cards it renders, and tries candidate endpoints — so the replacement is
chosen from evidence instead of guessed.
"""
from __future__ import annotations

import json
import re
import sys

import requests

SLUGS = ["Compass", "Criteo", "AppFolio", "Visa"]
HDR = {"accept": "*/*", "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def get(url: str, params: dict | None = None):
    try:
        return requests.get(url, params=params or {}, headers=HDR, timeout=30)
    except Exception as exc:
        print(f"      EXC {type(exc).__name__}: {exc}")
        return None


def count_json(r) -> str:
    if r is None:
        return "-"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception:
        return f"HTTP 200 non-json ({len(r.content)}b)"
    if isinstance(d, dict):
        for k in ("content", "postings", "jobs", "results"):
            if isinstance(d.get(k), list):
                return f"HTTP 200  totalFound={d.get('totalFound')}  len({k})={len(d[k])}"
        return f"HTTP 200 keys={list(d)[:6]}"
    if isinstance(d, list):
        return f"HTTP 200  list len={len(d)}"
    return "HTTP 200 ?"


def main() -> int:
    for slug in SLUGS:
        print(f"\n{'='*66}\n{slug}\n{'='*66}")

        html_resp = get(f"https://jobs.smartrecruiters.com/{slug}")
        html = html_resp.text if html_resp is not None else ""
        print(f"  careers page: HTTP {getattr(html_resp,'status_code','-')}  {len(html)} bytes")

        # How many job cards does the page itself render?
        for pat, label in (
            (r'opportunity-link', "opportunity-link"),
            (r'js-company-job', "js-company-job"),
            (r'/{}/\d{{6,}}'.format(re.escape(slug)), "posting-id links"),
        ):
            n = len(re.findall(pat, html))
            if n:
                print(f"     rendered job markers: {label} x{n}")

        # Any API hosts / paths referenced by the page.
        urls = set(re.findall(r'https?://[a-z0-9.\-]*smartrecruiters\.com/[A-Za-z0-9/_\-.?=&%{}]+', html))
        api_like = sorted(u for u in urls if any(k in u.lower() for k in ("api", "json", "postings", "search", "graphql")))
        if api_like:
            print("     API-ish URLs referenced by the page:")
            for u in api_like[:8]:
                print(f"        {u[:120]}")

        # Embedded JSON blobs that may hold the postings.
        for m in re.finditer(r'window\.(\w+)\s*=\s*(\{.{0,120})', html):
            print(f"     inline state: window.{m.group(1)} = {m.group(2)[:80]}...")

        print("  candidate endpoints:")
        cands = [
            ("v1 postings (current)", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", {"limit": 100}),
            ("v1 postings custom",    f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", {"limit": 100, "custom_field": ""}),
            ("careers /api/more",     f"https://jobs.smartrecruiters.com/{slug}/api/more", {"page": 0}),
            ("careers /api/groups",   f"https://jobs.smartrecruiters.com/{slug}/api/groups", None),
            ("search postings",       f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/search", {"limit": 100}),
        ]
        for label, url, params in cands:
            print(f"     {label:<24} {count_json(get(url, params))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
