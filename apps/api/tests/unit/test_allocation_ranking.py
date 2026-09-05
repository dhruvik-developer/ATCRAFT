"""Reallocation candidate ranking - no database involved.

Master rules section 13 requires the allocation priority to be configurable and
business-driven. That is only a real claim if the ranking is a pure function of
its inputs and a policy object, so this module pins down both halves:

* the *ordering* - a donor is a better candidate when it is less urgent, needed
  later, only a soft placeholder, and has had nothing issued against it;
* the *configurability* - the same candidates ranked under a different policy
  come back in a different order, and no comparison in the engine ignores the
  policy it was handed.

The eligibility rules get the same treatment, because "which reservations are
even offered" is the half that can quietly disappoint a customer: a demand that
is already in production, or one no less urgent than the requester, must never
appear in the list at all.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.modules.allocation.models import (
    DemandType,
    ReservationStatus,
    ReservationType,
)
from src.modules.allocation.repository import ReservationFacts
from src.modules.allocation.service import (
    DEFAULT_POLICY,
    ReallocationContext,
    ReallocationPolicy,
    candidate_eligibility,
    rank_candidates,
    score_candidate,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 1)
ITEM = uuid.uuid4()
WAREHOUSE = uuid.uuid4()


def _facts(**overrides: object) -> ReservationFacts:
    """A donor reservation: 100 units, untouched, HARD, priority 200, due in 30 days."""
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "item_id": ITEM,
        "warehouse_id": WAREHOUSE,
        "location_id": None,
        "batch_id": None,
        "demand_type": DemandType.SALES_ORDER,
        "demand_id": uuid.uuid4(),
        "demand_code": "SO/2026-27/00001",
        "reservation_type": ReservationType.HARD,
        "status": ReservationStatus.ACTIVE,
        "priority": 200,
        "required_date": TODAY + timedelta(days=30),
        "quantity": Decimal("100"),
        "consumed_quantity": Decimal("0"),
        "released_quantity": Decimal("0"),
        "remaining_quantity": Decimal("100"),
        "created_by_id": uuid.uuid4(),
        "claimed_quantity": Decimal("0"),
    }
    defaults.update(overrides)
    return ReservationFacts(**defaults)  # type: ignore[arg-type]


def _request(**overrides: object) -> ReallocationContext:
    """The urgent demand asking for stock: priority 10, needs 50, due in 5 days."""
    defaults: dict[str, object] = {
        "demand_type": DemandType.PRODUCTION_ORDER,
        "demand_id": uuid.uuid4(),
        "quantity_needed": Decimal("50"),
        "priority": 10,
        "required_date": TODAY + timedelta(days=5),
    }
    defaults.update(overrides)
    return ReallocationContext(**defaults)  # type: ignore[arg-type]


class TestEligibility:
    def test_a_donor_that_is_not_less_urgent_is_never_offered(self) -> None:
        """Equal urgency gives no claim. Allowing it would let two orders take
        turns reallocating from each other indefinitely."""
        request = _request(priority=100)
        reason = candidate_eligibility(
            _facts(priority=100), request=request, policy=DEFAULT_POLICY, as_of=TODAY
        )
        assert reason is not None
        assert "less urgent" in reason

    def test_a_donor_needed_inside_the_protection_window_is_not_offered(self) -> None:
        """The stand-in for "check the production start date": material for a job
        starting in two days has usually been kitted already."""
        facts = _facts(required_date=TODAY + timedelta(days=2))
        reason = candidate_eligibility(
            facts, request=_request(), policy=DEFAULT_POLICY, as_of=TODAY
        )
        assert reason is not None
        assert "protection window" in reason

    def test_a_donor_needed_just_outside_the_window_is_offered(self) -> None:
        facts = _facts(required_date=TODAY + timedelta(days=4))
        assert (
            candidate_eligibility(facts, request=_request(), policy=DEFAULT_POLICY, as_of=TODAY)
            is None
        )

    def test_the_requesting_demand_cannot_reallocate_from_itself(self) -> None:
        request = _request()
        facts = _facts(demand_type=request.demand_type, demand_id=request.demand_id, priority=900)
        reason = candidate_eligibility(facts, request=request, policy=DEFAULT_POLICY, as_of=TODAY)
        assert reason == "belongs to the requesting demand"

    def test_a_donor_already_claimed_by_another_request_is_not_offered_twice(self) -> None:
        """Two pending requests against the same reservation would produce an
        approval that cannot execute."""
        facts = _facts(remaining_quantity=Decimal("40"), claimed_quantity=Decimal("40"))
        reason = candidate_eligibility(
            facts, request=_request(), policy=DEFAULT_POLICY, as_of=TODAY
        )
        assert reason == "already claimed by another reallocation request"

    def test_a_partly_claimed_donor_is_offered_for_what_is_left(self) -> None:
        facts = _facts(remaining_quantity=Decimal("40"), claimed_quantity=Decimal("15"))
        assert (
            candidate_eligibility(facts, request=_request(), policy=DEFAULT_POLICY, as_of=TODAY)
            is None
        )
        assert facts.unclaimed_quantity == Decimal("25")

    def test_a_closed_reservation_is_not_a_donor(self) -> None:
        for status in (
            ReservationStatus.CONSUMED,
            ReservationStatus.RELEASED,
            ReservationStatus.EXPIRED,
        ):
            reason = candidate_eligibility(
                _facts(status=status), request=_request(), policy=DEFAULT_POLICY, as_of=TODAY
            )
            assert reason == f"reservation is {status}"

    def test_a_reservation_with_no_required_date_is_still_eligible(self) -> None:
        """A spare-part or safety-stock reservation has no date. Treating a
        missing date as "needed today" would protect exactly the reservations
        that are safest to take."""
        assert (
            candidate_eligibility(
                _facts(required_date=None),
                request=_request(),
                policy=DEFAULT_POLICY,
                as_of=TODAY,
            )
            is None
        )


class TestRanking:
    def test_the_softest_latest_least_urgent_donor_ranks_first(self) -> None:
        best = _facts(
            reservation_type=ReservationType.SOFT,
            priority=900,
            required_date=TODAY + timedelta(days=60),
        )
        middle = _facts(priority=400, required_date=TODAY + timedelta(days=40))
        worst = _facts(
            priority=200,
            required_date=TODAY + timedelta(days=10),
            consumed_quantity=Decimal("60"),
            remaining_quantity=Decimal("40"),
        )

        ranked = rank_candidates(
            [worst, middle, best], request=_request(quantity_needed=Decimal("300")), as_of=TODAY
        )
        assert [c.reservation_id for c in ranked] == [best.id, middle.id, worst.id]

    def test_a_soft_reservation_outranks_a_hard_one_of_the_same_urgency(self) -> None:
        """A planning placeholder should lose to a firm commitment every time:
        giving it up disappoints nobody."""
        soft = _facts(reservation_type=ReservationType.SOFT)
        hard = _facts(reservation_type=ReservationType.HARD)
        ranked = rank_candidates(
            [hard, soft], request=_request(quantity_needed=Decimal("300")), as_of=TODAY
        )
        assert ranked[0].reservation_id == soft.id
        assert ranked[0].requires_approval is False
        assert ranked[1].requires_approval is True

    def test_a_donor_with_material_already_issued_ranks_below_an_untouched_one(self) -> None:
        """Taking from a part-issued demand strands the material already on the
        shop floor."""
        untouched = _facts()
        started = _facts(consumed_quantity=Decimal("10"), remaining_quantity=Decimal("90"))
        ranked = rank_candidates(
            [started, untouched],
            request=_request(quantity_needed=Decimal("300")),
            as_of=TODAY,
        )
        assert ranked[0].reservation_id == untouched.id

    def test_a_donor_needed_sooner_than_the_requester_scores_below_zero_on_date(self) -> None:
        """Needing the material sooner is a reason not to take it, not merely an
        absence of a reason to take it."""
        request = _request(required_date=TODAY + timedelta(days=40))
        later, _ = score_candidate(
            _facts(required_date=TODAY + timedelta(days=50)),
            request=request,
            policy=DEFAULT_POLICY,
        )
        sooner, reasons = score_candidate(
            _facts(required_date=TODAY + timedelta(days=30)),
            request=request,
            policy=DEFAULT_POLICY,
        )
        assert sooner < later
        assert any("sooner" in reason for reason in reasons)

    def test_takeable_quantities_are_cumulative_and_stop_at_the_shortfall(self) -> None:
        """The list is an answer, not a menu the user has to add up."""
        first = _facts(priority=900, remaining_quantity=Decimal("60"))
        second = _facts(priority=800, remaining_quantity=Decimal("60"))
        third = _facts(priority=700, remaining_quantity=Decimal("60"))

        ranked = rank_candidates(
            [first, second, third],
            request=_request(quantity_needed=Decimal("100")),
            as_of=TODAY,
        )
        assert [c.takeable_quantity for c in ranked] == [Decimal("60"), Decimal("40")]
        assert len(ranked) == 2, "a third donor is not needed and must not be listed"

    def test_a_donor_only_offers_what_is_not_already_claimed(self) -> None:
        donor = _facts(remaining_quantity=Decimal("80"), claimed_quantity=Decimal("50"))
        ranked = rank_candidates(
            [donor], request=_request(quantity_needed=Decimal("80")), as_of=TODAY
        )
        assert ranked[0].takeable_quantity == Decimal("30")

    def test_nothing_is_suggested_when_nothing_is_needed(self) -> None:
        assert (
            rank_candidates([_facts()], request=_request(quantity_needed=Decimal("0")), as_of=TODAY)
            == []
        )

    def test_the_ranking_is_deterministic_regardless_of_input_order(self) -> None:
        """An approval screen that reshuffles between two identical requests is
        a screen nobody trusts."""
        pool = [
            _facts(priority=500, required_date=TODAY + timedelta(days=20)),
            _facts(priority=500, required_date=TODAY + timedelta(days=20)),
            _facts(priority=500, required_date=TODAY + timedelta(days=20)),
        ]
        request = _request(quantity_needed=Decimal("1000"))
        forward = [c.reservation_id for c in rank_candidates(pool, request=request, as_of=TODAY)]
        backward = [
            c.reservation_id
            for c in rank_candidates(list(reversed(pool)), request=request, as_of=TODAY)
        ]
        assert forward == backward
        assert len(forward) == 3

    def test_an_extreme_priority_cannot_swamp_every_other_consideration(self) -> None:
        """Every graded term is capped, so one absurd number cannot decide the
        ranking on its own."""
        capped = DEFAULT_POLICY.priority_gap_cap
        request = _request(priority=0)
        at_cap, _ = score_candidate(_facts(priority=capped), request=request, policy=DEFAULT_POLICY)
        far_past_cap, _ = score_candidate(
            _facts(priority=9999), request=request, policy=DEFAULT_POLICY
        )
        assert at_cap == far_past_cap


class TestPolicyIsTheSeam:
    def test_zeroing_the_soft_bonus_lets_priority_decide_instead(self) -> None:
        """The ordering is a property of the policy, not of the code: the same two
        candidates swap places when the business stops caring about softness."""
        soft_but_close = _facts(reservation_type=ReservationType.SOFT, priority=200)
        hard_but_distant = _facts(reservation_type=ReservationType.HARD, priority=205)
        pool = [hard_but_distant, soft_but_close]
        request = _request(quantity_needed=Decimal("300"))

        with_bonus = rank_candidates(pool, request=request, as_of=TODAY)
        assert with_bonus[0].reservation_id == soft_but_close.id

        without_bonus = rank_candidates(
            pool,
            request=request,
            policy=ReallocationPolicy(soft_reservation_bonus=Decimal("0")),
            as_of=TODAY,
        )
        assert without_bonus[0].reservation_id == hard_but_distant.id

    def test_excluding_hard_donors_removes_them_from_the_list_entirely(self) -> None:
        soft = _facts(reservation_type=ReservationType.SOFT)
        hard = _facts(reservation_type=ReservationType.HARD, priority=900)
        ranked = rank_candidates(
            [hard, soft],
            request=_request(quantity_needed=Decimal("1000")),
            policy=ReallocationPolicy(include_hard_donors=False),
            as_of=TODAY,
        )
        assert [c.reservation_id for c in ranked] == [soft.id]

    def test_a_wider_priority_gap_narrows_eligibility(self) -> None:
        """Raising the gap is how a business stops marginal differences from
        churning reservations."""
        facts = _facts(priority=15)
        request = _request(priority=10)
        assert (
            candidate_eligibility(facts, request=request, policy=DEFAULT_POLICY, as_of=TODAY)
            is None
        )
        assert (
            candidate_eligibility(
                facts,
                request=request,
                policy=ReallocationPolicy(min_priority_gap=10),
                as_of=TODAY,
            )
            is not None
        )

    def test_a_wider_protection_window_protects_more_donors(self) -> None:
        facts = _facts(required_date=TODAY + timedelta(days=10))
        assert (
            candidate_eligibility(facts, request=_request(), policy=DEFAULT_POLICY, as_of=TODAY)
            is None
        )
        assert (
            candidate_eligibility(
                facts,
                request=_request(),
                policy=ReallocationPolicy(protect_within_days=14),
                as_of=TODAY,
            )
            is not None
        )

    def test_max_candidates_caps_the_list_even_with_a_shortfall_left(self) -> None:
        pool = [_facts(priority=300 + index, remaining_quantity=Decimal("5")) for index in range(8)]
        ranked = rank_candidates(
            pool,
            request=_request(quantity_needed=Decimal("1000")),
            policy=ReallocationPolicy(max_candidates=3),
            as_of=TODAY,
        )
        assert len(ranked) == 3

    def test_the_eligibility_ceiling_matches_the_python_gap_rule(self) -> None:
        """The SQL filter and the Python rule are the same rule; if they drifted,
        the database would hand the ranking rows it then silently discards."""
        policy = ReallocationPolicy(min_priority_gap=5)
        ceiling = policy.eligible_priority_ceiling(100)
        # SQL keeps rows with priority > ceiling; the lowest such priority must
        # be exactly the first one Python accepts.
        assert ceiling == 104
        assert (
            candidate_eligibility(
                _facts(priority=105), request=_request(priority=100), policy=policy, as_of=TODAY
            )
            is None
        )
        assert (
            candidate_eligibility(
                _facts(priority=104), request=_request(priority=100), policy=policy, as_of=TODAY
            )
            is not None
        )


class TestExplanations:
    def test_every_candidate_explains_why_it_ranked_where_it_did(self) -> None:
        """The engine is asking somebody to disappoint a demand. A ranked list
        with no explanation has to be either rubber-stamped or redone by hand."""
        soft = _facts(reservation_type=ReservationType.SOFT, priority=800)
        ranked = rank_candidates(
            [soft], request=_request(quantity_needed=Decimal("50")), as_of=TODAY
        )
        reasons = " | ".join(ranked[0].reasons)
        assert "priority level(s) less urgent" in reasons
        assert "soft reservation" in reasons
        assert "no material issued against it yet" in reasons

    def test_a_hard_donor_says_it_needs_an_approved_request(self) -> None:
        ranked = rank_candidates(
            [_facts(reservation_type=ReservationType.HARD)],
            request=_request(quantity_needed=Decimal("50")),
            as_of=TODAY,
        )
        assert ranked[0].requires_approval is True
        assert any("needs an approved request" in reason for reason in ranked[0].reasons)

    def test_an_incomparable_date_is_stated_rather_than_guessed(self) -> None:
        _, reasons = score_candidate(
            _facts(required_date=None), request=_request(), policy=DEFAULT_POLICY
        )
        assert "no comparable required date" in reasons


class TestNamedDonor:
    """A planner who has already decided which demand should give way.

    ``from_reservation_id`` is refused unless the engine would have offered the
    reservation, and the list the engine returns is *cumulative* - it stops once
    the shortfall is covered. Those two facts together used to reject a donor
    that passed every eligibility rule, purely because a better-scoring one had
    already covered the quantity, and to tell the caller it was ineligible.
    """

    def test_an_eligible_donor_is_offered_even_when_a_better_one_covers_it_all(self) -> None:
        best = _facts(reservation_type=ReservationType.SOFT, priority=800)
        chosen = _facts(reservation_type=ReservationType.HARD, priority=300)
        request = _request(quantity_needed=Decimal("50"))

        # Unnamed, the soft donor covers the whole shortfall on its own.
        assert [
            c.reservation_id for c in rank_candidates([best, chosen], request=request, as_of=TODAY)
        ] == [best.id]

        ranked = rank_candidates(
            [best, chosen], request=request, as_of=TODAY, prefer_reservation_id=chosen.id
        )
        assert ranked[0].reservation_id == chosen.id
        assert ranked[0].takeable_quantity == Decimal("50.0000")

    @pytest.mark.parametrize(
        ("label", "overrides"),
        [
            ("more urgent than the requester", {"priority": 5}),
            ("inside the protection window", {"required_date": TODAY + timedelta(days=1)}),
            ("already claimed in full", {"claimed_quantity": Decimal("100")}),
            ("closed", {"status": ReservationStatus.RELEASED}),
        ],
    )
    def test_naming_a_donor_cannot_buy_past_eligibility(
        self, label: str, overrides: dict[str, object]
    ) -> None:
        """The promotion happens after eligibility, never through it - the
        request exists precisely so these rules are not bypassable by id."""
        donor = _facts(**overrides)
        ranked = rank_candidates(
            [donor],
            request=_request(quantity_needed=Decimal("50")),
            as_of=TODAY,
            prefer_reservation_id=donor.id,
        )
        assert ranked == [], label

    def test_an_unnamed_ranking_is_unchanged(self) -> None:
        """The promotion must not perturb the ordering when nobody named a donor."""
        pool = [
            _facts(priority=800, reservation_type=ReservationType.SOFT),
            _facts(priority=300),
            _facts(priority=250),
        ]
        request = _request(quantity_needed=Decimal("250"))
        assert [c.reservation_id for c in rank_candidates(pool, request=request, as_of=TODAY)] == [
            c.reservation_id
            for c in rank_candidates(pool, request=request, as_of=TODAY, prefer_reservation_id=None)
        ]
