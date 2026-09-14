"""
Undefined-name gate (the F821 class of bug), stdlib-only.

Two fleet-level bugs shipped in commit 69b46b6 because MagicMock-based tests
cannot see them: an unbound `adapter` made jam-ble-provisioning exit every
~9 s (BLE crash-loop -- the product's only recovery path), and an unbound
`threading` made WiFi priority promotion never run. Neither is a runtime
condition; both are visible in the source. This test walks every service's
symbol tables and fails on any name a function uses that is bound nowhere:
not in an enclosing scope, not at module level, not a builtin.

Pure `symtable`, so it runs on a JAM Player AND on a laptop without any Pi
library -- the only test in this tree that does not need the device.
"""
import builtins
import ast
import symtable
import unittest
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[1]

# Module-level dunders Python provides implicitly.
_MODULE_DUNDERS = {
    '__file__', '__name__', '__doc__', '__spec__', '__loader__', '__package__',
    '__builtins__', '__annotations__', '__path__', '__debug__',
}
_KNOWN = set(dir(builtins)) | _MODULE_DUNDERS


def _module_bindings(mod: symtable.SymbolTable) -> set:
    return {
        s.get_name() for s in mod.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_parameter()
    }


def _undefined_names(tab: symtable.SymbolTable, modnames: set, path: Path, out: list) -> None:
    """A name is undefined when a non-module scope reads it as an implicit
    global (bound in no enclosing function) and the module never binds it."""
    if tab.get_type() != 'module':
        for s in tab.get_symbols():
            n = s.get_name()
            if (s.is_global() and not s.is_declared_global() and not s.is_assigned()
                    and n not in modnames and n not in _KNOWN):
                out.append(f"{path.name}: '{tab.get_name()}' (def at line {tab.get_lineno()}) uses undefined name '{n}'")
    for child in tab.get_children():
        _undefined_names(child, modnames, path, out)


def _service_sources():
    # services_v2/*.py and common/*.py, plus the parent directory: in the repo
    # that is src/jam_player (constants, jam_enums, scenes_manager_service --
    # the content manager IS a deployed service); on a player it is /opt/jam,
    # which holds no .py files, so the extra glob is harmless there.
    files = (sorted(SERVICES_DIR.glob('*.py'))
             + sorted((SERVICES_DIR / 'common').glob('*.py'))
             + sorted(SERVICES_DIR.parent.glob('*.py')))
    return [f for f in files if f.name != '__init__.py']


def _has_star_import(src: str) -> bool:
    """
    A REAL `from x import *`, parsed -- not the characters appearing in a
    string or a comment.

    This was a substring test, and it silently excluded the single most
    safety-critical file in the product: jam_update.py embeds a copy of this
    very check as text for the updater's own pre-promotion validator, so the
    gate skipped it entirely. A newline lost from a constant there went
    unnoticed by every local run until an unrelated test tripped over it.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False   # let the caller report the parse failure
    return any(
        isinstance(node, ast.ImportFrom) and any(alias.name == '*' for alias in node.names)
        for node in ast.walk(tree)
    )

class NoUndefinedNamesTests(unittest.TestCase):

    def test_scans_a_meaningful_number_of_files(self):
        self.assertGreater(len(_service_sources()), 20, 'gate is not seeing the services tree')

    def test_no_function_uses_an_undefined_name(self):
        problems = []
        for f in _service_sources():
            src = f.read_text()
            if _has_star_import(src):
                continue  # star imports hide bindings from static analysis
            try:
                mod = symtable.symtable(src, str(f), 'exec')
            except SyntaxError as e:
                # A file that does not parse is a worse defect than any
                # undefined name, and it must fail HERE rather than surface as
                # a service that will not start.
                problems.append(f"{f.name}: does not parse -- {e}")
                continue
            _undefined_names(mod, _module_bindings(mod), f, problems)
        self.assertEqual(
            problems, [],
            'Undefined names (each is a NameError waiting for that code path):\n  ' + '\n  '.join(problems),
        )


    def test_a_file_is_not_skipped_for_the_words_import_star_in_a_string(self):
        """The regression that hid a broken jam_update.py from every run."""
        self.assertFalse(_has_star_import("CHECK = \"if 'import *' in src\"\n"))
        self.assertTrue(_has_star_import("from os.path import *\n"))

    def test_every_service_file_parses(self):
        broken = []
        for f in _service_sources():
            try:
                ast.parse(f.read_text())
            except SyntaxError as e:
                broken.append(f"{f.name}: {e}")
        self.assertEqual(broken, [], 'files that will not even import:\n  ' + '\n  '.join(broken))

    def test_gate_catches_the_bug_class(self):
        """Self-check: the detector must flag an unbound name in a nested function."""
        src = (
            "def main():\n"
            "    def tick():\n"
            "        return adapter.Get('x')\n"   # adapter bound nowhere
            "    return tick\n"
        )
        mod = symtable.symtable(src, '<selfcheck>', 'exec')
        out = []
        _undefined_names(mod, _module_bindings(mod), Path('selfcheck.py'), out)
        self.assertEqual(len(out), 1, out)
        self.assertIn("'adapter'", out[0])

    def test_gate_accepts_enclosing_scope_and_module_bindings(self):
        """Self-check: closures over an enclosing local and module globals are fine."""
        src = (
            "import os\n"
            "LIMIT = 3\n"
            "def main():\n"
            "    adapter = object()\n"
            "    def tick():\n"
            "        return adapter, os, LIMIT, len\n"
            "    return tick\n"
        )
        mod = symtable.symtable(src, '<selfcheck>', 'exec')
        out = []
        _undefined_names(mod, _module_bindings(mod), Path('selfcheck.py'), out)
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
