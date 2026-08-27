"""Keyword sets for the Project Coordinator track.

This track runs completely separately from the data-roles and software flows:
its own database, its own schedule, its own email. Nothing here is imported by
the other pipelines, and the classifier only consults these lists when the
active track has been switched to "coordinator".

Why a separate track at all: "coordinator" appears in none of the data or
software keyword lists, so every Project Coordinator posting scored 0 and was
dropped by the title gate in main.py before it was ever stored. Adding the
words to the data lists would have mixed them into the main digest instead.
"""
from __future__ import annotations

# Unambiguous project/program coordination titles. A hit scores high.
#
# Matched with _has_keyword, which allows flexible separators and an optional
# trailing plural, so "Project Coordinator", "Project-Coordinator" and
# "Project Coordinators" all hit the same entry.
COORDINATOR_STRONG = [
    # The core family
    "project coordinator",
    "program coordinator",
    "project co-ordinator",
    "programme coordinator",
    "projects coordinator",
    "project management coordinator",
    "pmo coordinator",
    "pmo analyst",
    "pmo specialist",
    # Same job under a different noun
    "project administrator",
    "program administrator",
    "project specialist",
    "program specialist",
    "project analyst",
    "program analyst",
    "project associate",
    "program associate",
    "project assistant",
    "program assistant",
    "project support specialist",
    "project support analyst",
    # Planning/controls flavours of the same work
    "project controls analyst",
    "project controls specialist",
    "project scheduler",
    "project planner",
    "planning coordinator",
    "scheduling coordinator",
    "resource coordinator",
    "implementation coordinator",
    "operations coordinator",
    "business operations coordinator",
]

# Adjacent titles that are a step up from coordinator. These score lower and
# generally surface as "maybe" — the shared experience gate in evaluation.py
# blocks anything whose description asks for more than 3 years, so the ones
# that survive are the genuinely junior openings.
#
# Deliberately NOT included: "business analyst" and "operations analyst". Both
# already appear in the data track's keyword lists, and the two tracks keep
# separate databases with no shared dedup, so a single posting matching both
# would arrive twice — one copy in the data digest and one here.
COORDINATOR_WEAK = [
    "project manager",
    "program manager",
    "associate project manager",
    "assistant project manager",
    "junior project manager",
    "project lead",
    "implementation specialist",
    "scrum master",
    "agile coordinator",
]

# Coordination work in domains that are a different profession entirely.
# These are not "office project coordination with a different adjective" —
# they are clinical, trade or field roles that happen to share the noun, and
# a candidate cannot apply to them off the back of a project-coordination CV.
COORDINATOR_HARD_EXCLUDES = [
    "patient care",
    "nurse",
    "nursing",
    "clinical care",
    "care coordinator",
    "surgical",
    "pharmacy",
    "veterinary",
    "welding",
    "electrical foreman",
    "hvac",
    "plumbing",
    "flight attendant",
    "cdl",
    "truck driver",
]

# Titles that belong to the other two tracks. A posting is scored by exactly
# one track, and the data/software flows already cover these.
OTHER_TRACK_EXCLUDES = [
    "data engineer",
    "data scientist",
    "software engineer",
    "software developer",
    "machine learning engineer",
    "devops engineer",
    "site reliability engineer",
]

# Level markers that read as genuinely entry-level. Small lift, same idea as
# the software track: the point of this search is the junior end of the range.
# Named distinctly from the software track's list so the classifier, which
# imports both, can never pick up the wrong one.
COORDINATOR_EARLY_CAREER_SIGNALS = [
    "i",
    "1",
    "entry level",
    "entry-level",
    "associate",
    "assistant",
    "junior",
    "jr",
    "new grad",
    "university grad",
    "graduate",
    "trainee",
    "apprentice",
]

# Shown in the digest email footer and used by evaluation.py to score how well
# a job description lines up with what this track is actually looking for.
COORDINATOR_TARGET_ROLES = [
    "Project Coordinator",
    "Program Coordinator",
    "Project Administrator",
    "Project Analyst",
    "Project Specialist",
    "PMO Analyst",
    "Project Scheduler",
    "Operations Coordinator",
    "Associate Project Manager",
]
