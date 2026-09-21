"""The ``:resume`` pager: pick a recorded session and reinstate its history.

Kept as a plain function taking ``(agent, session_logger)`` — not ``ctx`` —
because ``miniagent/tests/test_sessions.py`` drives it directly with that
exact two-argument signature to script the picker through scripted
``input()`` answers (paging, cancel, EOF and retry). The registered
``:resume`` command in :mod:`miniagent.console.commands` is a thin wrapper
that calls this with ``ctx.agent`` and ``ctx.session_logger``.
"""

from __future__ import annotations

import time

from ..sessions import SessionError

_RESUME_PAGE_SIZE = 4


def resume_command(agent, session_logger):
    """Handle ``:resume`` — pick a recorded session and reinstate its history.

    Sessions are listed four per page.  Choosing 1-4 resumes the session at
    that position on the current page; 0 and 5 turn to the previous and next
    page; a blank answer cancels.
    """
    sessions = session_logger.list_sessions()
    if not sessions:
        print("No recorded sessions for this workspace yet.")
        return

    page = 0
    total_pages = (len(sessions) + _RESUME_PAGE_SIZE - 1) // _RESUME_PAGE_SIZE
    while True:
        start = page * _RESUME_PAGE_SIZE
        entries = sessions[start:start + _RESUME_PAGE_SIZE]
        print()
        print(f"Recorded sessions (page {page + 1} of {total_pages}):")
        for i, info in enumerate(entries, start=1):
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(info.last_activity))
            print(f"  {i}. {info.id}  ({info.message_count} messages, "
                  f"last {when})")
            if info.preview:
                print(f"      {info.preview}")
        nav_left = "0. previous page" if page > 0 else "0. (first page)"
        nav_right = "5. next page" if page + 1 < total_pages else "5. (last page)"
        print(f"  {nav_left}    {nav_right}    Enter to cancel")

        try:
            choice = input("Resume> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        if not choice.isdigit():
            print(f"Enter 0-{_RESUME_PAGE_SIZE}: 1-4 picks a session, "
                  "0/5 turn pages.")
            continue

        picked = int(choice)
        if picked == 0:
            page = max(0, page - 1)
            continue
        if picked == _RESUME_PAGE_SIZE + 1:
            if page + 1 < total_pages:
                page += 1
            else:
                print("Already on the last page.")
            continue
        if 1 <= picked <= len(entries):
            _resume_session(agent, session_logger, entries[picked - 1])
            return
        print(f"This page lists {len(entries)} session(s); "
              f"enter 1-{len(entries)}.")


def _resume_session(agent, session_logger, info):
    """Load session *info*, attach the logger to it, and restore the history."""
    try:
        history = session_logger.load(info.id)
        session_logger.attach(info.id)
    except SessionError as exc:
        print(f"Cannot resume session {info.id}: {exc}")
        return
    agent.restore(history)
    print(f"Resumed session {info.id}: {len(history)} messages restored.")
    print("The conversation continues from where it left off; new messages")
    print("are appended to this session's log.")
