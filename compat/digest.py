"""Transitional: keeps `python /app/digest.py` working in the image.

Deployments written before the code moved to src/thelounge_digest still run
the old script path. This runs the thelounge-digest console script's module
instead; remove compat/ once nothing uses `python /app/digest.py`.
"""

import runpy

runpy.run_module("thelounge_digest.digest", run_name="__main__", alter_sys=True)
