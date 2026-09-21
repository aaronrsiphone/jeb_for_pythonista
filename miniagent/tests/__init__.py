"""MiniAgent's in-package test suite.

Each ``test_*.py`` here is a standalone script that is safe to run through
the agent's ``run_python`` tool: no network, no console loop, no interactive
prompts (prompt-driven code is exercised with scripted inputs — see
``docs/testing.md``), and no writes outside the system temp directory.  The
tests re-import the package fresh so the current source files are exercised.

Run the whole suite with::

    run_python miniagent/tests/run_all.py

Tests live here permanently.  Add new tests to this folder (or extend an
existing one) instead of creating scratch test files that get deleted
afterwards — every fix keeps its regression test.
"""
