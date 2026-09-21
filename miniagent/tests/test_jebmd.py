"""Tests for the JEB.md discovery + merge engine (miniagent/jebmd.py).

No network; everything lives under temp directories that stand in for the
home directory (patched via jebmd._global_jeb_md_paths, since the real merge
engine reads Path.home() directly) and a throwaway project workspace. Lives
permanently in miniagent/tests/; run it directly or through run_all.py.
"""

import shutil
import sys
import tempfile
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent import jebmd  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


tmp_roots = []  # cleaned in finally

try:
    # --- _split_jeb_md_sections / _render_jeb_md_sections --------------------

    text = """\
# Heading One

Some preamble text
that continues.

- item one
- item two

# Heading Two

more text
"""
    sections = jebmd._split_jeb_md_sections(text)
    check("section count", len(sections), 2)
    check("first heading", sections[0][0], "# Heading One")
    # a run of prose is one unit, but each list item is its own unit (a new
    # list-item line always flushes whatever came before it).
    check("first section unit count", len(sections[0][1]), 3)
    check("first unit is not a list item", sections[0][1][0][0], False)
    check("first unit text",
          sections[0][1][0][1], "Some preamble text\nthat continues.")
    check("second unit is a list item", sections[0][1][1][0], True)
    check("second unit text", sections[0][1][1][1], "- item one")
    check("third unit is a list item", sections[0][1][2][0], True)
    check("third unit text", sections[0][1][2][1], "- item two")
    check("second heading", sections[1][0], "# Heading Two")

    rendered = jebmd._render_jeb_md_sections(sections)
    check("round-trips through split/render",
          jebmd._split_jeb_md_sections(rendered), sections)

    # preamble with no heading
    no_heading = jebmd._split_jeb_md_sections("just text\nmore text")
    check("text with no heading has one section", len(no_heading), 1)
    check("that section's heading is empty", no_heading[0][0], "")

    empty = jebmd._split_jeb_md_sections("")
    check("empty text has no sections", empty, [])

    # --- unit/heading keys are whitespace- and case-insensitive --------------

    check("unit_key collapses whitespace and case",
          jebmd._unit_key("  Some   Text\nhere "), "some text here")
    check("heading_key strips leading hashes",
          jebmd._heading_key("## My Heading"), "my heading")

    # --- horizontal rule detection --------------------------------------------

    check("--- is a horizontal rule", jebmd._is_hr_unit("---"), True)
    check("*** is a horizontal rule", jebmd._is_hr_unit("***"), True)
    check("plain text is not a horizontal rule",
          jebmd._is_hr_unit("- not a rule"), False)
    check("too-short dashes are not a rule", jebmd._is_hr_unit("--"), False)

    # --- _merge_global_jeb_md_texts: duplicate elision ------------------------

    docs_text = "# Rules\n\n- always run tests\n- never force-push"
    legacy_dup = "# Rules\n\n- always run tests"
    merged, conflicts = jebmd._merge_global_jeb_md_texts(docs_text, legacy_dup)
    check("exact duplicate unit is not duplicated", merged.count("always run tests"), 1)
    check("no conflicts recorded for an exact duplicate", conflicts, [])

    # --- conflict resolution: Documents version wins --------------------------

    docs_text2 = "# Rules\n\n- never delete the checkpoints directory"
    legacy_conflict = "# Rules\n\n- never delete the checkpoint directory please"
    merged2, conflicts2 = jebmd._merge_global_jeb_md_texts(docs_text2, legacy_conflict)
    check("conflicting legacy unit dropped from merged text",
          "checkpoint directory please" in merged2, False)
    check("Documents unit kept in merged text",
          "never delete the checkpoints directory" in merged2, True)
    check("one conflict recorded", len(conflicts2), 1)
    check("conflict records the dropped legacy unit",
          "checkpoint directory please" in conflicts2[0][0], True)
    check("conflict records the kept Documents unit",
          "checkpoints directory" in conflicts2[0][1], True)

    # --- sections unique to one file are kept ---------------------------------

    docs_text3 = "# Only In Docs\n\nsome docs-only content"
    legacy_text3 = "# Only In Legacy\n\nsome legacy-only content"
    merged3, conflicts3 = jebmd._merge_global_jeb_md_texts(docs_text3, legacy_text3)
    check("docs-only section kept", "some docs-only content" in merged3, True)
    check("legacy-only section appended", "some legacy-only content" in merged3, True)
    check("docs section still comes first",
          merged3.index("Only In Docs") < merged3.index("Only In Legacy"), True)
    check("no conflicts between disjoint sections", conflicts3, [])

    # --- unit unique to a shared section is appended within it ---------------

    docs_text4 = "# Rules\n\n- rule A"
    legacy_text4 = "# Rules\n\n- rule A\n- rule B (legacy only)"
    merged4, conflicts4 = jebmd._merge_global_jeb_md_texts(docs_text4, legacy_text4)
    check("shared unit kept once", merged4.count("rule A"), 1)
    check("legacy-only unit within a shared section is appended",
          "rule B (legacy only)" in merged4, True)
    check("no conflict for an additional (non-similar) unit", conflicts4, [])

    # --- empty inputs ----------------------------------------------------------

    merged_empty, conflicts_empty = jebmd._merge_global_jeb_md_texts("", "")
    check("merging two empty texts yields empty text", merged_empty, "")
    check("merging two empty texts yields no conflicts", conflicts_empty, [])

    # --- _read_jeb_md: missing file, trimming, truncation ---------------------

    home = Path(tempfile.mkdtemp(prefix="ma_jebmd_home_"))
    tmp_roots.append(home)

    missing = home / "does-not-exist.md"
    check("reading a missing file returns empty string",
          jebmd._read_jeb_md(missing), "")

    padded = home / "padded.md"
    padded.write_text("\n\n  hello world  \n\n", encoding="utf-8")
    check("read text is trimmed", jebmd._read_jeb_md(padded), "hello world")

    huge = home / "huge.md"
    huge.write_text("x" * (jebmd._MAX_JEB_MD_CHARS + 500), encoding="utf-8")
    huge_text = jebmd._read_jeb_md(huge)
    check("oversized file is truncated",
          len(huge_text) < jebmd._MAX_JEB_MD_CHARS + 500, True)
    check("truncation note mentions how much was cut",
          "truncated" in huge_text, True)

    # --- load_jeb_md_context: global + local assembly -------------------------

    docs_dir = home / "Documents" / "miniagent"
    legacy_dir = home / "miniagent"
    docs_dir.mkdir(parents=True)
    legacy_dir.mkdir(parents=True)

    project = Path(tempfile.mkdtemp(prefix="ma_jebmd_project_"))
    tmp_roots.append(project)

    # monkeypatch Path.home() so jebmd's global lookup uses our temp tree
    original_home = Path.home
    Path.home = classmethod(lambda cls: home)  # type: ignore[assignment]
    try:
        # nothing present at all
        check("no files -> empty context", jebmd.load_jeb_md_context(project), "")

        # only the local file
        (project / jebmd.JEB_MD_NAME).write_text(
            "Local instructions only.", encoding="utf-8")
        only_local = jebmd.load_jeb_md_context(project)
        check("local-only context includes the local marker",
              "# Project JEB.md (workspace)" in only_local, True)
        check("local-only context has no global marker",
              "Global JEB.md" in only_local, False)
        check("local-only context includes the local text",
              "Local instructions only." in only_local, True)

        # only the Documents global file
        (docs_dir / jebmd.JEB_MD_NAME).write_text(
            "# Rules\n\n- docs global rule", encoding="utf-8")
        docs_only = jebmd.load_jeb_md_context(project)
        check("docs-only global header used",
              "# Global JEB.md (~/Documents/miniagent)" in docs_only, True)
        check("docs-only context includes both global and local text",
              ("docs global rule" in docs_only,
               "Local instructions only." in docs_only), (True, True))
        check("global section precedes local section",
              docs_only.index("docs global rule")
              < docs_only.index("Local instructions only."), True)

        # both global files present, no conflict
        (legacy_dir / jebmd.JEB_MD_NAME).write_text(
            "# Other Rules\n\n- legacy-only rule", encoding="utf-8")
        both = jebmd.load_jeb_md_context(project)
        check("merged-global header used when both global files exist",
              "# Global JEB.md (~/Documents/miniagent merged with ~/miniagent)"
              in both, True)
        check("merged context carries both global rules",
              ("docs global rule" in both, "legacy-only rule" in both),
              (True, True))

        # both global files present, with a genuine conflict
        (docs_dir / jebmd.JEB_MD_NAME).write_text(
            "# Rules\n\n- never touch the config file directly",
            encoding="utf-8")
        (legacy_dir / jebmd.JEB_MD_NAME).write_text(
            "# Rules\n\n- never touch the config file directly please",
            encoding="utf-8")
        text, header, conflicts = jebmd._load_global_jeb_md()
        check("conflict header still the merged one",
              header,
              "# Global JEB.md (~/Documents/miniagent merged with ~/miniagent)")
        check("one conflict resolved in favour of Documents", len(conflicts), 1)
        check("Documents wording kept",
              "never touch the config file directly" in text, True)
        check("legacy wording (with 'please') dropped",
              "directly please" in text, False)
    finally:
        Path.home = original_home

    # --- _global_jeb_md_paths shape --------------------------------------------

    paths = jebmd._global_jeb_md_paths()
    check("two candidate global paths returned", len(paths), 2)
    check("first candidate is the Documents version",
          paths[0].parts[-3:], ("Documents", "miniagent", "JEB.md"))
    check("second candidate is the legacy version",
          paths[1].parts[-2:], ("miniagent", "JEB.md"))

finally:
    for root in tmp_roots:
        shutil.rmtree(root, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} jebmd self-test check(s) failed")
print("All checks passed.")
