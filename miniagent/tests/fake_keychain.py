"""An in-memory keychain that keeps tests away from the real one.

On Pythonista, ``miniagent.keys`` stores API keys in the device keychain
under fixed service names (``MiniAgent:brave``, ``MiniAgent:provider:NAME``).
A test that drives ``:key set brave`` or ``:rotate-key`` against the real
keychain overwrites the user's real keys with test values.  Tests that touch
key storage call :func:`isolate_keychain` first, which points
``keys._get_keychain`` at a :class:`FakeKeychain`, and call the returned
``restore`` in their ``finally``.

Off-device there is no keychain module, so without this the keys code falls
back to environment variables; with it, both platforms exercise the same
keychain code path.  Not a test itself (no ``test_`` prefix), so
``run_all.py`` does not run it.
"""


class FakeKeychain:
    """The subset of Pythonista's ``keychain`` module that ``keys.py`` uses."""

    def __init__(self):
        self.passwords = {}

    def get_password(self, service, account):
        return self.passwords.get((service, account))

    def set_password(self, service, account, password):
        self.passwords[(service, account)] = password

    def delete_password(self, service, account):
        self.passwords.pop((service, account), None)


def isolate_keychain(keys_module):
    """Point *keys_module* at a fresh FakeKeychain.

    Returns ``(fake, restore)``; call ``restore()`` to put the original
    ``_get_keychain`` back.
    """
    fake = FakeKeychain()
    original = keys_module._get_keychain
    keys_module._get_keychain = lambda: fake

    def restore():
        keys_module._get_keychain = original

    return fake, restore
