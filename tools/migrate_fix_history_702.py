"""One-off migration script for Issue #702 — kept for the audit trail, not for reuse.

Reads RELEASE_NOTES/KNOWN_FIXES from a pre-#702 checkout of const.py, applies the
compaction rules from the approved plan, and emits:
  - fix_history.jsonl (the compacted unified records)
  - a review report (stdout) summarizing what happened to every source entry

Does NOT modify const.py — it was run once against the last const.py revision that
still had RELEASE_NOTES/KNOWN_FIXES, the report was reviewed, and the two dicts were
then deleted from const.py by hand. Running this against the current const.py (which
no longer has those dicts) will fail; that's expected. Kept so a future reader can see
exactly how the historical corpus was reshaped and compacted.
"""

import importlib.util
import json
import os
import re
import sys

CONST_PATH = sys.argv[1] if len(sys.argv) > 1 else "custom_components/climate_advisor/const.py"
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else "fix_history.jsonl"

spec = importlib.util.spec_from_file_location("const", CONST_PATH)
const = importlib.util.module_from_spec(spec)
spec.loader.exec_module(const)

KNOWN_FIXES: dict[int, dict] = const.KNOWN_FIXES
RELEASE_NOTES: dict[str, list[str]] = const.RELEASE_NOTES

BULLET_PREFIX = re.compile(r"^(?:Fix|Feat) #([0-9/#]+):")


def parse_version(v: str) -> tuple[int, ...]:
    v = str(v).split("-")[0].split("/")[0]
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


# ---- Step 1: index every bullet by issue number(s) it references ----
# bullets_by_issue[n] = list of (version, bullet_text)
bullets_by_issue: dict[int, list[tuple[str, str]]] = {}
all_bullets = 0
freeform_bullets = 0
for ver, bullets in RELEASE_NOTES.items():
    for b in bullets:
        all_bullets += 1
        m = BULLET_PREFIX.match(b)
        if not m:
            freeform_bullets += 1
            continue
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        for n in nums:
            bullets_by_issue.setdefault(n, []).append((ver, b))


# Decision 2 (approved plan): multi-issue bullets ("Fix #721/#722: ...") attach to
# every referenced issue number, not just an exact single-number prefix match. Uses
# bullets_by_issue (built above) which already indexes every number a bullet mentions.
def exact_bullet(issue_num: int, version_fixed: str) -> str:
    for ver, note in bullets_by_issue.get(issue_num, []):
        if ver == version_fixed:
            return note
    return ""


# ---- Step 2: detect the strangler-fig epic cluster ----
EPIC_PATTERNS = [
    re.compile(r"strangler-fig", re.IGNORECASE),
    re.compile(r"Phase \d+ of", re.IGNORECASE),
]


def is_epic_member(issue_num: int) -> bool:
    fix = KNOWN_FIXES.get(issue_num, {})
    text = str(fix.get("title", "")) + " " + str(fix.get("scope_covered", ""))
    return any(p.search(text) for p in EPIC_PATTERNS)


epic_candidates = sorted(n for n in KNOWN_FIXES if is_epic_member(n))
# #731 (fan FSM extraction) is the precursor work for the same strangler-fig
# program the regex-detected phases (735-757) later graduate/remove the legacy
# path for -- its own text doesn't say "Phase N of"/"strangler-fig" literally but
# it's the same initiative family per the plan's human-reviewed cluster (confirmed
# by reading its title: "the same gap the nat-vent/door-window/override-grace FSM
# extractions already closed"). Included manually since regex alone won't catch it.
if 731 in KNOWN_FIXES and 731 not in epic_candidates:
    epic_candidates = sorted([*epic_candidates, 731])

# ---- Step 3: no-user-visible-change issue set (for individual trim) ----
novis_issues = set()
for _ver, bullets in RELEASE_NOTES.items():
    for b in bullets:
        if "no user-visible change" in b:
            m = BULLET_PREFIX.match(b)
            if m:
                for n in re.findall(r"\d+", m.group(1)):
                    novis_issues.add(int(n))

# ---- Build output records ----
records = []
report_lines = []

EPIC_LABEL = "strangler-fig-fsm-migration"
if epic_candidates:
    # version_fixed values may themselves be ranges (e.g. "0.6.66-0.6.69") — take
    # the first version token of each for the start, last token for the end.
    starts = []
    ends = []
    for n in epic_candidates:
        vf = str(KNOWN_FIXES[n].get("version_fixed", ""))
        parts = vf.split("-")
        starts.append(parse_version(parts[0]))
        ends.append(parse_version(parts[-1]))
    version_start = ".".join(str(x) for x in min(starts))
    version_end = ".".join(str(x) for x in max(ends))
    merged_summary = (
        f"Strangler-fig FSM migration ({', '.join('#' + str(n) for n in epic_candidates)}): "
        "internal refactor migrating automation subsystems (fan/WHF, economizer, "
        "override/grace, door/window, occupancy, classification) from legacy dual-path "
        "logic to a single FSM-based implementation, in sequential phases across "
        f"versions {version_start}-{version_end}. No user-visible "
        "behavior change at any phase - each cutover was verified against zero "
        "corpus/shadow-comparison divergence before the legacy path was removed."
    )
    epic_record = {
        "issue": epic_candidates[0],
        "version_fixed": f"{version_start}-{version_end}",
        "title": merged_summary,
        "scope_covered": "automation.py, coordinator.py: legacy-to-FSM cutover across every automation subsystem",
        "user_summary": None,
        "merged_from": epic_candidates,
    }
    records.append(epic_record)
    report_lines.append(f"EPIC MERGE: {len(epic_candidates)} entries -> 1 record")
    report_lines.append(f"  merged_from: {epic_candidates}")
    report_lines.append(f"  proposed title: {merged_summary}")
    report_lines.append("")

epic_set = set(epic_candidates)
pre05_count = 0
trimmed_novis_count = 0
kept_asis_count = 0

for n in sorted(KNOWN_FIXES.keys()):
    if n in epic_set:
        continue
    fix = KNOWN_FIXES[n]
    version_fixed = fix.get("version_fixed", "")
    title = fix.get("title", "")
    scope_covered = fix.get("scope_covered", "")
    user_summary = exact_bullet(n, version_fixed) or None

    if parse_version(version_fixed) < (0, 5, 0):
        # Aggressive trim: keep issue/version/one-line title only.
        one_line = str(title).split(". ")[0].split("\n")[0]
        if len(one_line) > 200:
            one_line = one_line[:197] + "..."
        records.append(
            {
                "issue": n,
                "version_fixed": version_fixed,
                "title": one_line,
                "scope_covered": None,
                "user_summary": user_summary,
            }
        )
        pre05_count += 1
    elif n in novis_issues:
        # Individual trim: scope_covered -> one sentence.
        one_line = str(scope_covered).split(". ")[0].split("\n")[0]
        if len(one_line) > 200:
            one_line = one_line[:197] + "..."
        records.append(
            {
                "issue": n,
                "version_fixed": version_fixed,
                "title": title,
                "scope_covered": one_line,
                "user_summary": user_summary,
            }
        )
        trimmed_novis_count += 1
    else:
        records.append(
            {
                "issue": n,
                "version_fixed": version_fixed,
                "title": title,
                "scope_covered": scope_covered,
                "user_summary": user_summary,
            }
        )
        kept_asis_count += 1

# ---- Orphan bullets: RELEASE_NOTES references an issue# with no KNOWN_FIXES entry ----
orphan_nums = sorted(set(bullets_by_issue.keys()) - set(KNOWN_FIXES.keys()))
for n in orphan_nums:
    ver, bullet = bullets_by_issue[n][0]
    records.append(
        {
            "issue": n,
            "version_fixed": ver,
            "title": None,
            "scope_covered": None,
            "user_summary": bullet,
            "orphan": True,
        }
    )

# ---- Write output ----
with open(OUT_PATH, "w", encoding="utf-8") as f:
    for rec in records:
        # drop None-valued keys to keep lines compact
        clean = {k: v for k, v in rec.items() if v is not None}
        f.write(json.dumps(clean, ensure_ascii=False) + "\n")

# ---- Report ----
out_size = os.path.getsize(OUT_PATH)
print("=" * 70)
print("MIGRATION REPORT")
print("=" * 70)
print(f"Source KNOWN_FIXES entries: {len(KNOWN_FIXES)}")
print(f"Source RELEASE_NOTES versions: {len(RELEASE_NOTES)} ({all_bullets} bullets, {freeform_bullets} freeform)")
print()
print(f"Output records: {len(records)}")
print(f"  - Epic-merged: {len(epic_candidates)} -> 1 record" if epic_candidates else "  - Epic-merged: none")
print(f"  - Pre-0.5.x aggressively trimmed: {pre05_count}")
print(f"  - No-user-visible-change individually trimmed: {trimmed_novis_count}")
print(f"  - Kept as-is (substantive, post-0.5.x): {kept_asis_count}")
print(f"  - Orphan bullets (no KNOWN_FIXES entry): {len(orphan_nums)} -> {orphan_nums}")
print()
accounted = len(epic_candidates) + pre05_count + trimmed_novis_count + kept_asis_count
status = "OK" if accounted == len(KNOWN_FIXES) else "MISMATCH!!"
print(f"Accounted for: {accounted} / {len(KNOWN_FIXES)} KNOWN_FIXES entries", status)
print()
print(f"Output file size: {out_size} bytes ({out_size / 1024:.1f} KB)")
print("\n".join(report_lines))
