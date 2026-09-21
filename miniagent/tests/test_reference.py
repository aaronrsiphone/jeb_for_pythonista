"""The anti-drift test for docs/reference.md (§5.7 of docs/rearchitecture.md).

A generator that nobody is forced to re-run is just a script someone forgets
about, and the stale doc it should have replaced comes right back. This is
the check that actually prevents that: it regenerates the reference in
memory with ``gendocs.render_reference()`` and asserts the result matches
the checked-in ``miniagent/docs/reference.md`` byte for byte. Whenever a tool,
a command, or a module docstring changes without the checked-in file being
regenerated, this test fails — with a message telling the reader exactly how
to fix it, not just that something is wrong.

Safe to run via run_python: no network, no writes (it only reads the
checked-in file and compares), and no console loop.  Lives permanently in
miniagent/tests/; run it directly or through run_all.py.
"""

import sys
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these new modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent import gendocs  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


REFERENCE_PATH = Path(__file__).resolve().parent.parent / "docs" / "reference.md"

regenerated = gendocs.render_reference()

check("docs/reference.md exists", REFERENCE_PATH.exists(), True)

if REFERENCE_PATH.exists():
    checked_in = REFERENCE_PATH.read_text(encoding="utf-8")
    if checked_in != regenerated:
        failures.append("docs/reference.md matches the generator, byte for byte")
        print(
            "FAIL: docs/reference.md matches the generator, byte for byte\n"
            "  docs/reference.md is out of date. Regenerate it with:\n"
            "      run_python miniagent/gendocs.py\n"
            "  then re-run this test (or the suite) to confirm it now matches."
        )
    else:
        print("PASS: docs/reference.md matches the generator, byte for byte")

    # A second regeneration must produce the exact same text: the generator
    # itself must be deterministic, or "matches the checked-in file" would be
    # a coin flip rather than a guarantee.
    check("the generator is deterministic across two runs",
          gendocs.render_reference(), regenerated)

    # Pin down the sections a reader (or an agent trusting this file) relies
    # on actually being present, not just "some bytes matched".
    for heading in ("## Tools (", "## Commands (", "## Module layout ("):
        check(f"reference.md contains a {heading!r} section",
              heading in checked_in, True)
    check("reference.md carries the generated-file warning",
          "GENERATED FILE" in checked_in, True)
    check("reference.md points at the regeneration command",
          "run_python miniagent/gendocs.py" in checked_in, True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} reference-doc check(s) failed")
print("All checks passed.")
