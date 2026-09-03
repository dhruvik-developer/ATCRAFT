"""Celery task modules.

Each file here registers the tasks for one area of work
(``mrp``, ``reports``, ``notifications``, ``maintenance``) and is listed in
``celery_app.conf.include`` so a worker discovers them at start-up.

Nothing is imported here. A task module opens database sessions and pulls in the
service layer, and importing all of them from the package root would make any
process that merely touches ``src.workers.tasks`` pay for every one of them - the
API among them, which dispatches tasks by *name* precisely so it never has to
import the worker code.
"""

from __future__ import annotations

__all__: list[str] = []
