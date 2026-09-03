"""Explicit state machines for business documents.

Master rules sections 18/73: important entities have explicit state machines and
invalid transitions are rejected by the backend. Encoding them as data - rather
than as ``if`` chains scattered through services - means the allowed graph can be
tested, rendered in the UI, and documented from one place.

Every module that owns a document defines its own machine and calls
:meth:`StateMachine.assert_transition` inside the same transaction that performs
the change, so a rejected transition rolls back with everything else.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission

S = TypeVar("S", bound=StrEnum)


@dataclass(frozen=True, slots=True)
class Transition(Generic[S]):
    """One legal edge in the state graph."""

    source: S
    target: S
    #: Permission the actor must hold. ``None`` means any user who can update
    #: the document may perform it.
    permission: Permission | None = None
    #: Human-readable label for the UI action button.
    label: str = ""
    #: True when the transition writes stock, money or another irreversible
    #: effect. Used by the UI to demand confirmation and by tests to enforce
    #: that such transitions are always audited.
    is_irreversible: bool = False


@dataclass(frozen=True)
class StateMachine(Generic[S]):
    """A validated transition graph for one document type."""

    entity: str
    initial: S
    transitions: tuple[Transition[S], ...]
    terminal: frozenset[S] = field(default_factory=frozenset)

    def allowed_targets(self, current: S) -> list[S]:
        return [t.target for t in self.transitions if t.source == current]

    def find(self, current: S, target: S) -> Transition[S] | None:
        for t in self.transitions:
            if t.source == current and t.target == target:
                return t
        return None

    def can_transition(self, current: S, target: S) -> bool:
        return self.find(current, target) is not None

    def assert_transition(self, current: S, target: S) -> Transition[S]:
        """Raise :class:`InvalidStateTransitionError` unless the edge exists."""
        transition = self.find(current, target)
        if transition is None:
            raise InvalidStateTransitionError(
                entity=self.entity,
                current=str(current),
                requested=str(target),
                allowed=[str(s) for s in self.allowed_targets(current)],
            )
        return transition

    def required_permission(self, current: S, target: S) -> Permission | None:
        transition = self.assert_transition(current, target)
        return transition.permission

    def is_terminal(self, state: S) -> bool:
        return state in self.terminal

    def available_actions(self, current: S, held: Iterable[Permission]) -> list[Transition[S]]:
        """Transitions this actor may actually perform - drives the UI buttons.

        The frontend uses this for affordances only; the backend re-checks on
        the request itself (section 38).
        """
        held_set = set(held)
        return [
            t
            for t in self.transitions
            if t.source == current and (t.permission is None or t.permission in held_set)
        ]

    def to_graph(self) -> dict[str, list[str]]:
        """Adjacency map - used by documentation and by the UI status stepper."""
        graph: dict[str, list[str]] = {}
        for t in self.transitions:
            graph.setdefault(str(t.source), []).append(str(t.target))
        return graph

    def validate(self) -> None:
        """Structural self-check, exercised by the unit tests.

        Catches the two mistakes that actually happen: a state nothing can reach,
        and a non-terminal state nothing can leave (a document stuck forever).
        """
        sources = {t.source for t in self.transitions}
        targets = {t.target for t in self.transitions}
        states = sources | targets | {self.initial}

        unreachable = states - targets - {self.initial}
        if unreachable:
            raise ValueError(f"{self.entity}: unreachable states {sorted(map(str, unreachable))}")

        dead_ends = {s for s in states if s not in sources and s not in self.terminal}
        if dead_ends:
            raise ValueError(
                f"{self.entity}: non-terminal states with no exit {sorted(map(str, dead_ends))}"
            )


__all__ = ["StateMachine", "Transition"]
