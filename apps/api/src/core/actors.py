"""Who did it, when nobody did: the system actor.

Some writes have no person behind them. A Celery beat sweep that repairs a
document stranded by an older release, or that expires a stale reservation, runs
with no request and therefore no principal - yet every service it calls was
written for one, and :class:`~src.core.deps.TenantContext` requires a user id.

Such a sweep carries :data:`SYSTEM_ACTOR_ID`, the nil UUID. It is a placeholder,
never a user: ``users`` has no such row, so writing it into ``created_by_id``,
``updated_by_id`` or an audit row's actor would violate the foreign key and roll
the sweep back. :func:`actor_id` is the one translation from "who is acting" to
"what to stamp", and the base repository and the audit writer both go through it,
so a service needs no special case to run as the system: its stamps come out
NULL and its audit rows say :data:`SYSTEM_ACTOR_LABEL` instead of naming a
person.

Deliberately free of imports from the rest of the application: the repository
base class, the audit writer and the dependency module all read it, and it must
not pull any of them in.
"""

from __future__ import annotations

import uuid

#: The actor of work no person started. Never a real user; see the module
#: docstring for why it must never reach a foreign key.
SYSTEM_ACTOR_ID = uuid.UUID(int=0)

#: What an audit row says in place of a person's name when the system acted.
SYSTEM_ACTOR_LABEL = "System"


def actor_id(user_id: uuid.UUID | None) -> uuid.UUID | None:
    """The id to stamp into an actor column: ``None`` for the system actor."""
    if user_id is None or user_id == SYSTEM_ACTOR_ID:
        return None
    return user_id


def is_system_actor(user_id: uuid.UUID | None) -> bool:
    """Whether ``user_id`` is the system placeholder rather than a person."""
    return user_id == SYSTEM_ACTOR_ID


__all__ = ["SYSTEM_ACTOR_ID", "SYSTEM_ACTOR_LABEL", "actor_id", "is_system_actor"]
