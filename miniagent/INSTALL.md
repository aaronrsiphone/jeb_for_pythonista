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
        agent.py
        config.py
        permissions.py
        provider.py
        runner.py
        tools.py
        workspace.py
```

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

## Where MiniAgent stores its own state

MiniAgent keeps its configuration and permission policy **outside** your
projects, in an application-state directory derived at runtime.  It prefers:

```
~/Documents/miniagent/
    config.json
    permissions.json
```

falling back to `~/.miniagent/` and then a temp directory.  The API key is
stored in the Pythonista keychain, never in a JSON file.  You can set it from
the MiniAgent console with:

```
:key set sk-your-key-here
```

## Updating

To update, just overwrite the `miniagent/` directory in site-packages with the
new version.  Your `config.json`, `permissions.json`, and keychain key are
untouched.
