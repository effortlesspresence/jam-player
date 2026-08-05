"""
Shared venv completeness check -- STDLIB ONLY.

Single source of truth for "is the /opt/jam/venv usable": requirements.txt.
Used by BOTH jam_update.py (prevention: don't commit a broken venv) and
jam_venv_repair.py (self-heal: repair a broken venv on boot), so the set of
packages we verify is never duplicated -- it is read from requirements.txt at
runtime.

The actual verification runs INSIDE the target venv (subprocess to the venv's
python) and uses importlib.metadata -- the same mechanism pip uses to decide
a requirement is "already satisfied", so it correctly sees system-site
packages (e.g. dbus-python from apt) as well as pip-installed ones.

HARD RULE: standard library only. jam_venv_repair imports this while the venv
is (possibly) broken, so it must never pull in a third-party or venv package.
"""
import subprocess
from pathlib import Path

# Runs inside the target venv. Reads the requirements file passed as argv[1]
# -- the ONLY source of truth for the dependency set -- and reports any pinned
# distribution that isn't installed. Prints "MISSING: a, b" and exits 1 if so.
_CHECK_SNIPPET = r'''
import sys, importlib.metadata as md


def norm(v):
    # Tolerant numeric normalization: "1.0" == "1.0.0", "01.2" == "1.2".
    # Returns None when the version is not purely dotted integers.
    out = []
    for part in v.strip().lower().split("."):
        if not part.isdigit():
            return None
        out.append(int(part))
    while out and out[-1] == 0:
        out.pop()
    return tuple(out)


problems = []
with open(sys.argv[1]) as fh:
    for raw in fh:
        # Strip comments and environment markers first.
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        # Capture the exact pin when present (plain == only; === is
        # arbitrary-equality and other specifiers are not version-exact).
        pin = None
        if "==" in line and "===" not in line:
            left, right = line.split("==", 1)
            pin = right.strip()
            name = left
        else:
            name = line
            for sep in (">=", "<=", "~=", "!=", "===", ">", "<"):
                if sep in name:
                    name = name.split(sep, 1)[0]
                    break
        name = name.split("[", 1)[0].strip()
        if not name:
            continue
        try:
            installed = md.version(name)  # PEP 503-normalizes the name
        except md.PackageNotFoundError:
            problems.append(name)
            continue
        # Version check for == pins: presence alone let a failed pin-bump
        # pass the gate, commit the version marker, and strand the device on
        # new code with old deps -- permanently, since the healer shares this
        # check. Deliberately FAIL-OPEN when either version does not
        # normalize (rc/post/epoch tags): a false mismatch would loop the
        # updater/healer on a reinstall pip already considers satisfied,
        # which is worse than the narrow window fail-open leaves.
        if pin:
            a, b = norm(installed), norm(pin)
            if a is not None and b is not None and a != b:
                problems.append(name + "==" + pin + "(installed:" + installed + ")")

if problems:
    sys.stdout.write("MISSING: " + ", ".join(problems))
    sys.exit(1)
sys.exit(0)
'''


def missing_requirements(venv_python, requirements, timeout: int = 60):
    """Return the list of requirements.txt distributions NOT installed in the
    venv. Empty list == the venv is complete. A non-empty list -- including a
    sentinel like '<venv-python-missing>' -- means NOT complete, so callers can
    uniformly treat any non-empty result as "needs repair / fail closed".
    """
    vp, rq = Path(venv_python), Path(requirements)
    if not vp.exists():
        return ['<venv-python-missing>']
    if not rq.exists():
        return ['<requirements-missing>']
    try:
        result = subprocess.run(
            [str(vp), '-c', _CHECK_SNIPPET, str(rq)],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        return [f'<check-error:{e}>']
    if result.returncode == 0:
        return []
    out = (result.stdout or '').strip()
    if out.startswith("MISSING:"):
        return [p.strip() for p in out[len("MISSING:"):].split(",") if p.strip()]
    # Ran but reported failure without our marker -- treat as incomplete.
    return [f'<check-failed:{(result.stderr or "").strip()[-160:]}>']
