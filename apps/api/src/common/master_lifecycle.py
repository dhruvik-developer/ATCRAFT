"""The activate / deactivate / retire lifecycle every master table shares.

A master row has no ``status`` column: its state is derived from ``is_active``
and ``deleted_at``, the two flags the rest of the system already reads, and a
third, redundant column could disagree with them. Routing the moves through a
:class:`~src.core.state_machine.StateMachine` still gives one place where the
legal transitions are declared, tested and rendered (master rules section 18).

One definition rather than a copy per module, because a copy is how two masters
end up disagreeing about whether a retired row may come back: the work-centre
and storage-location copies each declared an ARCHIVED -> INACTIVE "Restore"
edge that no route or service method performs, which also kept ARCHIVED
non-terminal in both.

The item master keeps its own ``ItemLifecycleState``: the API publishes that
name in the contract, so it is not folded in here for consistency alone.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from src.core.permissions import Permission
from src.core.state_machine import StateMachine, Transition


class MasterLifecycleState(StrEnum):
    """Lifecycle of a master row, derived from its flags by :func:`master_state`."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


class FlaggedMaster(Protocol):
    """The two columns a master's lifecycle state is derived from."""

    @property
    def is_active(self) -> bool: ...

    @property
    def deleted_at(self) -> datetime | None: ...


def master_state(record: FlaggedMaster) -> MasterLifecycleState:
    """Derive the lifecycle state of a master row from its flags.

    Retirement wins over deactivation: a retired row that still carries
    ``is_active=True`` from before the retirement never reads as usable.
    """
    if record.deleted_at is not None:
        return MasterLifecycleState.ARCHIVED
    return MasterLifecycleState.ACTIVE if record.is_active else MasterLifecycleState.INACTIVE


def master_lifecycle(
    entity: str,
    manage: Permission,
    *,
    retire: Permission | None = None,
    restorable: bool = False,
) -> StateMachine[MasterLifecycleState]:
    """Activate, deactivate and retire, with retirement terminal unless ``restorable``.

    ``restorable`` is for a master that has a restore endpoint: it adds the
    ARCHIVED -> INACTIVE "Restore" edge, and a restored row comes back
    INACTIVE, never ACTIVE, so someone looks at it before it is used again.
    Without one ARCHIVED is terminal, because an edge out of it would advertise
    a transition no caller can make.

    ``retire`` is the permission retirement needs when it is not ``manage`` - a
    customer's delete right, say.
    """
    retire_permission = manage if retire is None else retire
    transitions = [
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.INACTIVE,
            permission=manage,
            label="Deactivate",
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ACTIVE,
            permission=manage,
            label="Activate",
        ),
        # Retiring stops every new use of the row, so it is confirmed as
        # irreversible even where a restore exists.
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=retire_permission,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=retire_permission,
            label="Retire",
            is_irreversible=True,
        ),
    ]
    if restorable:
        transitions.append(
            Transition(
                MasterLifecycleState.ARCHIVED,
                MasterLifecycleState.INACTIVE,
                permission=manage,
                label="Restore",
            )
        )
    return StateMachine(
        entity=entity,
        initial=MasterLifecycleState.ACTIVE,
        transitions=tuple(transitions),
        terminal=frozenset() if restorable else frozenset({MasterLifecycleState.ARCHIVED}),
    )


__all__ = ["FlaggedMaster", "MasterLifecycleState", "master_lifecycle", "master_state"]
