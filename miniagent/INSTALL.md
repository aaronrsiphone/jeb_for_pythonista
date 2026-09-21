# Installing MiniAgent in Pythonista

MiniAgent is a pure-Python package with no third-party dependencies beyond
what Pythonista already ships (`requests`).  Installation is a one-time manual
copy of the `miniagent/` directory into Pythonista's user site-packages.

## 1. Find your user site-packages directory

Run this in any Pythonista script (or the interactive console):

```python
import sys, site
print(site.getusersitepackages())
print([p for p in sys.path if "site-packages" in p])
```

One of the printed paths is the **user site-packages** directory.  In
Pythonista this is also the folder visible in the file browser under
**Python Modules** (a "site-packages" entry).  It looks something like:

```
~/Documents/site-packages
```

(Do not copy the iOS container UUID from your machine — derive the path with
the snippet above so the instructions stay device-independent.)

## 2. Copy the package

Copy the entire `miniagent/` directory (the one containing `__init__.py`,
`app.py`, `agent.py`, etc.) into that site-packages directory so you end up
with:

```
site-packages/
    miniagent/
        __init__.py
        app.py
        context.py
        agent.py
        events.py
        checkpoints.py
        jebmd.py
        keys.py
        config.py
        permissions.py
        provider.py
        runner.py
        sessions.py
        vision.py
        workspace.py
        knowledge.py
        gendocs.py
        _jeb.py
        JEB.md
        template_JEB.md
        tools/
            __init__.py
            registry.py
            dispatch.py
            list_files.py
            read_file.py
            search_files.py
            create_file.py
            edit_file.py
            multi_edit.py
            overwrite_file.py
            clean_up.py
            run_python.py
            ask_image.py
            knowledge.py
        console/
            __init__.py
            loop.py
            registry.py
            resume.py
            commands/
                __init__.py
                help_cmds.py
                config_cmds.py
                perms_cmds.py
                session_cmds.py
                workspace_cmds.py
        ui/
            __init__.py
            console.py
            verbose.py
            headless.py
        tests/
            __init__.py
            run_all.py
            test_*.py
        docs/
            architecture.md
            self_editing.md
            testing.md
            jeb_md.md
            reference.md
            rearchitecture.md
```

Copy the whole tree — every subpackage (`tools/`, `console/`, `console/commands/`,
`ui/`) needs its own `__init__.py` to import correctly.

You can do this with Pythonista's own file browser (drag the `miniagent`
folder into **Python Modules**), or with the Files app.

## 3. Verify the install

Run, in a fresh Pythonista console or any script:

```python
import miniagent
print(miniagent)
from miniagent import run
print(run)
```

If both lines print without error, the package is importable and ready.
To also confirm behavior end to end, run the bundled test suite
(`miniagent/tests/run_all.py`) — every test is self-contained, needs no
network, and writes only to throwaway directories under the system temp
folder.

## 4. Use it in any project

Copy the tiny `jeb.py` file into any project directory and run it:

```
SomeProject/
    jeb.py
    foo.py
    data/
        example.json
```

```python
# jeb.py — identical in every project, never edited
from pathlib import Path
from miniagent import run
if __name__ == "__main__":
    run(project_root=Path(__file__).resolve().parent)
```

Running `jeb.py` starts an interactive MiniAgent console scoped to
`SomeProject/`.

Alternatively, copy `miniagent/_jeb.py` from the installed package to
`jeb.py` in your project instead of typing it out:

```
cp <site-packages>/miniagent/_jeb.py <project>/jeb.py
```

(replace `<site-packages>` with the directory from step 1, and `<project>`
with your project directory). This is the exact same snippet shown above,
saved as a template inside the package so you don't have to retype it —
handy if you're scripting the setup. If you installed via the Files app or
Pythonista's own file browser, hand-typing the snippet above (or copying it
with the file browser) may be more convenient than a shell `cp`.

## Where MiniAgent stores its own state

MiniAgent keeps its configuration and permission policy **outside** your
projects, in an application-state directory derived at runtime.  It prefers:

```
~/Documents/miniagent/
    config.json
    permissions.json
    sessions/
    checkpoints/
```

falling back to `~/.miniagent/` and then a temp directory.  `sessions/`
holds the recorded conversation logs (`:resume`) for every workspace, one
JSONL file per session.  `checkpoints/` holds the per-turn file snapshots
`:undo` restores from, also one directory per workspace.  The API key is
stored in the Pythonista keychain, never in a JSON file.  You can set it from
the MiniAgent console with:

```
:key set sk-your-key-here
```

## Updating

To update, just overwrite the `miniagent/` directory in site-packages with the
new version.  Your `config.json`, `permissions.json`, and keychain key are
untouched.
