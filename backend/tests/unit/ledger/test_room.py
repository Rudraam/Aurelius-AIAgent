"""Contribution room (P9, invariant 7): withdrawals never increase remaining room within a year."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ledger.models import REGISTERED, AccountKind
from app.ledger.room import MovementType, RoomEntry, RoomMovement, remaining_room, room_statuses

kinds = st.sampled_from(sorted(REGISTERED))
amounts = st.integers(min_value=1, max_value=1_000_000).map(lambda c: Decimal(c).scaleb(-2))
days = st.dates(min_value=date(2026, 6, 1), max_value=date(2028, 6, 30))
movements = st.builds(RoomMovement, kind=kinds, on=days, amount=amounts, movement=st.sampled_from(list(MovementType)))
entries = st.builds(
    RoomEntry,
    kind=kinds,
    year=st.just(2027),
    room=st.integers(-100_000, 5_000_000).map(lambda c: Decimal(c).scaleb(-2)),
    as_of=st.dates(min_value=date(2027, 1, 1), max_value=date(2027, 12, 31)),
    source_note=st.just("CRA My Account (fictional)"),
)


@settings(max_examples=300, deadline=None)
@given(entries, st.lists(movements, max_size=12), st.lists(movements, min_size=1, max_size=6))
def test_withdrawals_never_increase_remaining_room(
    entry: RoomEntry, history: list[RoomMovement], extra: list[RoomMovement]
) -> None:
    withdrawals = [m.model_copy(update={"movement": MovementType.WITHDRAWAL}) for m in extra]
    assert remaining_room(entry, [*history, *withdrawals]) == remaining_room(entry, history)


@settings(max_examples=300, deadline=None)
@given(entries, st.lists(movements, max_size=12), movements)
def test_any_movement_never_increases_remaining_room(
    entry: RoomEntry, history: list[RoomMovement], extra: RoomMovement
) -> None:
    assert remaining_room(entry, [*history, extra]) <= remaining_room(entry, history)


def entry(kind: AccountKind = AccountKind.TFSA, room: str = "1000.00", as_of: date = date(2027, 2, 1)) -> RoomEntry:
    return RoomEntry(kind=kind, year=2027, room=Decimal(room), as_of=as_of, source_note="fictional")


def move(
    on: date, amount: str, movement: MovementType = MovementType.CONTRIBUTION, kind: AccountKind = AccountKind.TFSA
) -> RoomMovement:
    return RoomMovement(kind=kind, on=on, amount=Decimal(amount), movement=movement)


def test_counts_contributions_on_or_after_as_of_in_the_same_year_and_type() -> None:
    history = [
        move(date(2027, 1, 31), "100.00"),  # before as-of: already reflected in the entered room
        move(date(2027, 2, 1), "200.00"),  # on as-of: counted (conservative)
        move(date(2027, 3, 1), "50.00", MovementType.WITHDRAWAL),  # never adds room
        move(date(2027, 4, 1), "25.00", kind=AccountKind.FHSA),  # other type
        move(date(2028, 1, 5), "300.00"),  # next year
    ]
    assert remaining_room(entry(), history) == Decimal("800.00")


def test_over_contribution_is_negative_and_room_statuses_picks_latest_entry() -> None:
    older = entry(room="500.00", as_of=date(2027, 1, 1))
    newer = entry(room="300.00", as_of=date(2027, 6, 1))
    statuses = room_statuses([older, newer, entry(AccountKind.FHSA, "0.00")], [move(date(2027, 6, 2), "400.00")], 2027)
    by_kind = {s.kind: s.remaining for s in statuses}
    assert by_kind == {AccountKind.FHSA: Decimal("0.00"), AccountKind.TFSA: Decimal("-100.00")}
    assert room_statuses([older], [], 2028) == ()  # no entry for 2028: stale
