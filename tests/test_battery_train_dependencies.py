"""train() must be able to run at all.

`train_battery_model` fired on 2026-08-10 and died with
ModuleNotFoundError: No module named 'numpy'. The import carried the comment
"available via pyarrow", which stopped being true at pyarrow 16.0 — numpy
became an optional dependency there, and nothing else in requirements.txt
pulls it in.

That was the LAST link in a chain of four: the wrong trip anchor meant almost
no observations were ever mined, the 10-30 minute window (calibrated for the
retired 10-minute cadence) rejected the few that were, and even had both been
right, the fit itself could not import.

The point of this file is that "the fit is unfittable" should fail in CI, not
silently once a week in a cron job whose output nobody reads.
"""

from __future__ import annotations

import pathlib


def test_numpy_is_importable():
    """The direct assertion. train() does `import numpy as np` lazily, so a
    missing numpy is invisible to every other test in the suite."""
    import numpy  # noqa: F401


def test_numpy_is_a_declared_dependency_not_a_transitive_one():
    """Importable in the dev venv is not the same as installed in the image.
    numpy must be in requirements.txt on its own account — relying on another
    package to drag it in is exactly what broke."""
    req = (pathlib.Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    declared = [ln.split("==")[0].split(">=")[0].strip()
                for ln in req.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    assert "numpy" in declared


def test_train_can_solve_a_least_squares_system():
    """Exercises the actual call train() makes, on data with known
    coefficients, so a numpy that imports but cannot solve is caught too."""
    import numpy as np

    rng = np.random.default_rng(0)
    x1 = rng.uniform(400, 8000, 400)      # distance m
    x2 = rng.uniform(0, 120, 400)         # elevation gain m
    x3 = rng.uniform(-5, 35, 400)         # temperature C
    y = 2.0 + 0.0015 * x1 + 0.05 * x2 - 0.1 * x3

    design = np.column_stack([np.ones(len(x1)), x1, x2, x3])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)

    assert beta[0] == __import__("pytest").approx(2.0, abs=1e-6)
    assert beta[1] == __import__("pytest").approx(0.0015, abs=1e-9)
    assert beta[2] == __import__("pytest").approx(0.05, abs=1e-9)
    assert beta[3] == __import__("pytest").approx(-0.1, abs=1e-9)
