"""Run every test_*.py in this folder sequentially and summarise.

Usage (from the workspace root, e.g. through the agent's run_python tool)::

    run_python miniagent/tests/run_all.py

Each test prints its own PASS/FAIL lines; this runner adds a per-test
verdict and a final summary.  A test that raises (they all raise
AssertionError when a check fails) counts as failed, and the runner itself
raises at the end if anything failed, so a failing suite is always visible
in the run result.  Each test remains runnable on its own.
"""

import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    tests = sorted(HERE.glob("test_*.py"))
    if not tests:
        print(f"No test_*.py files found in {HERE}")
        return

    print(f"MiniAgent test suite — {len(tests)} file(s) in {HERE}")
    print()

    failed = []
    for path in tests:
        print("=" * 64)
        print(path.name)
        print("=" * 64)
        try:
            runpy.run_path(str(path), run_name="__main__")
        except Exception as exc:
            failed.append((path.name, exc))
            print(f"RUNNER: {path.name} FAILED — {exc}")
        else:
            print(f"RUNNER: {path.name} PASSED")
        print()

    print("=" * 64)
    if failed:
        print(f"{len(failed)} of {len(tests)} test file(s) FAILED:")
        for name, exc in failed:
            print(f"  - {name}: {exc}")
        raise AssertionError(
            f"{len(failed)} test file(s) failed: "
            + ", ".join(name for name, _ in failed)
        )
    print(f"All {len(tests)} test file(s) passed.")


if __name__ == "__main__":
    main()
