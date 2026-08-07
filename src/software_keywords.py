"""Keyword sets for the Software Developer track.

This track runs completely separately from the data-roles flow: its own
database, its own schedule, its own email. Nothing here is imported by the
data pipeline, and the classifier only consults these lists when the active
track has been switched to "software".

Targets early-career software roles — the experience gate lives in
evaluation.py and blocks anything asking for more than 3 years.
"""
from __future__ import annotations

# Unambiguous software-engineering titles. A hit scores high.
SOFTWARE_STRONG = [
    # Core titles
    "software developer",
    "software engineer",
    "software development engineer",
    "software engineering",
    "application developer",
    "applications developer",
    "application engineer",
    "programmer analyst",
    "programmer",
    "sde",
    "swe",
    # Stack-oriented
    "backend developer",
    "backend engineer",
    "back end developer",
    "back-end engineer",
    "frontend developer",
    "frontend engineer",
    "front end developer",
    "front-end engineer",
    "full stack developer",
    "full stack engineer",
    "fullstack developer",
    "fullstack engineer",
    "full-stack developer",
    "full-stack engineer",
    "web developer",
    "web engineer",
    # Mobile
    "mobile developer",
    "mobile engineer",
    "ios developer",
    "ios engineer",
    "android developer",
    "android engineer",
    # Adjacent build-the-product engineering
    "api developer",
    "api engineer",
    "platform engineer",
    "systems engineer",
    "systems developer",
    "embedded software",
    "embedded engineer",
    "devops engineer",
    "site reliability engineer",
    "cloud engineer",
    "qa engineer",
    "quality assurance engineer",
    "test engineer",
    "automation engineer",
    "software test",
    # Language-specific titles
    "java developer",
    "python developer",
    "javascript developer",
    "typescript developer",
    "react developer",
    "node developer",
    "node.js developer",
    "golang developer",
    "go developer",
    "c# developer",
    ".net developer",
    "dotnet developer",
    "ruby developer",
    "php developer",
    "salesforce developer",
]

# Softer signals — score lower, usually need context to be worth surfacing.
SOFTWARE_WEAK = [
    "developer",
    "engineer i",
    "engineer 1",
    "associate engineer",
    "junior engineer",
    "software",
    "engineering",
    "coding",
    "development engineer",
    "technical analyst",
    "solutions engineer",
    "integration engineer",
    "release engineer",
    "build engineer",
    "infrastructure engineer",
]

# Data-domain roles. These belong to the data flow and must not appear in the
# software track, or the two searches return the same jobs. Without this,
# "Data Platform Engineer" and "Data Scientist, Application Developer" both
# scored 90 here purely on the "platform engineer"/"application developer"
# substrings.
DATA_DOMAIN_EXCLUDES = [
    "data engineer",
    "data engineering",
    "data scientist",
    "data science",
    "data analyst",
    "data analytics",
    "data platform",
    "analytics engineer",
    "machine learning",
    "ml engineer",
    "mlops",
    "business intelligence",
    "bi developer",
    "bi engineer",
    "data architect",
    "database administrator",
    "etl developer",
    "data warehouse",
    "data visualization",
    "analytics",
    "bi analyst",
    "business analyst",
    "snowflake",
    "databricks",
    "decision scientist",
]

# People-management and high-level IC titles. A 0-3 year search should never
# surface these, and the shared seniority cap only demotes them to "maybe" —
# real postings like "Software Development Manager" were still coming through.
MANAGEMENT_EXCLUDES = [
    "manager",
    "director",
    "head of",
    "vp",
    "vice president",
    "principal",
    "staff engineer",
    "distinguished",
    "fellow",
    "architect",
]

# Titles that merely contain "engineer"/"developer" but are not software roles.
# Checked before the weak list so civil/mechanical/sales roles do not leak in.
SOFTWARE_HARD_EXCLUDES = [
    "civil engineer",
    "mechanical engineer",
    "electrical engineer",
    "chemical engineer",
    "structural engineer",
    "industrial engineer",
    "aerospace engineer",
    "manufacturing engineer",
    "process engineer",
    "field engineer",
    "sales engineer",
    "project engineer",
    "facilities engineer",
    "environmental engineer",
    "petroleum engineer",
    "biomedical engineer",
    "nuclear engineer",
    "hvac",
    "engineer technician",
    "engineering technician",
    "business developer",
    "business development",
    "land developer",
    "real estate developer",
    "learning and development",
    "training and development",
]

# Early-career signals. These are welcome — the track targets 0-3 years.
# Internships stay excluded by the shared HARD_EXCLUDE_REGEXES.
EARLY_CAREER_SIGNALS = [
    "new grad",
    "new graduate",
    "university grad",
    "university graduate",
    "college grad",
    "recent grad",
    "early career",
    "entry level",
    "entry-level",
    "junior",
    "associate",
    "graduate engineer",
    "grad program",
    "rotational program",
    "i",  # e.g. "Software Engineer I" — matched as a word, see classifier
]

# Roles this track should surface, used for target-alignment scoring.
SOFTWARE_TARGET_ROLES = [
    "Software Developer",
    "Software Engineer",
    "Backend Engineer",
    "Frontend Engineer",
    "Full Stack Engineer",
    "Web Developer",
    "Application Developer",
    "Mobile Developer",
    "Platform Engineer",
    "DevOps Engineer",
    "Cloud Engineer",
    "Site Reliability Engineer",
    "QA Engineer",
    "Test Automation Engineer",
]
