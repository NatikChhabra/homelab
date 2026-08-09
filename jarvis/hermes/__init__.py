"""
Hermes: specialist routing for a 3B model with a hard tool cliff.

This file exists to make `hermes` a REAL package rather than a namespace one.
Measured 2026-08-09: without it, `import hermes` resolves to whichever of
`/app/hermes/` (the directory) or `/app/hermes/hermes.py` (the module) happens
to come first on sys.path, which depends on the working directory of whatever
started the process. Running a test from inside the directory made
`from hermes.hermes import Hermes` fail with "'hermes' is not a package", and
because router.py catches import errors and degrades to the big agent, the only
visible symptom was Hermes silently switching itself off.
"""
