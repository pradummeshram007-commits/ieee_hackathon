"""The shared, live planning world: trips, locks, re-planning and scoped views.

One process holds one :class:`WorldState`, guarded by ``_STATE_LOCK`` and
persisted to ``app_state`` after every change. A monotonic ``version`` counter
is bumped on each change; every browser polls it and re-renders only when it
moves.

The important rule, and the reason a plain "solve everything" is not enough:

    **A re-plan must never disturb a trip a driver has already accepted.**

So a re-planning round removes the locked trucks, the farmers riding on them,
and the buyer demand those trips already cover, and plans only what is left.
The locked routes are then merged back in untouched. :func:`_solver_input` and
:func:`_merge_plan` are the two halves of that.

Solving takes seconds, so re-plans run on a background thread behind
``_PLAN_LOCK``: at most one runs at a time, at most one more is queued, and
while either is true every view is told ``planning: true`` so it can say
"Re-optimising...". Reads never block on the planner.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel, Field

from . import baseline as baseline_planner
from . import db as database
from . import metrics
from .auth import create_user
from .models import (
    AcceptStatus,
    Buyer,
    Dataset,
    Farmer,
    LotStatus,
    Notification,
    PlanResult,
    Role,
    RouteResult,
    StopKind,
    StopStatus,
    Trip,
    TripStatus,
    UnservedFarmer,
    UnservedReason,
    UserPublic,
    minutes_to_clock,
)
from .seed import DEMO_PASSWORD, build_dataset, demo_accounts
from .solver import solve

#: Solver budget for every automatic re-plan. Tests turn this down.
PLAN_TIME_LIMIT = float(os.environ.get("KISANROUTE_TIME_LIMIT", "8"))

#: Defaults applied when a farmer posts a lot without spelling out the times.
DEFAULT_PACK_TIME_MIN = 30
DEFAULT_PICKUP_WINDOW_MIN = 360


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorldState(BaseModel):
    """Everything persisted in the ``app_state`` JSON document."""

    dataset: Dataset
    #: The merged optimised plan: locked routes plus whatever was planned around them.
    plan: Optional[PlanResult] = None
    #: The naive comparison, kept so the coordinator dashboard has something to show.
    baseline: Optional[PlanResult] = None
    trips: Dict[str, Trip] = Field(default_factory=dict)
    #: username -> newest-first inbox, capped so the document cannot grow forever.
    notifications: Dict[str, List[Notification]] = Field(default_factory=dict)
    seed: int = 42
    disruptions: List[str] = Field(default_factory=list)
    last_planned_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #
_STATE: Optional[WorldState] = None
_VERSION: int = 0
_STATE_LOCK = threading.RLock()
_PLAN_LOCK = threading.Lock()
_planning_depth = 0
_replan_queued = False


class WorkflowError(Exception):
    """Raised for rule violations; main.py turns these into HTTP errors."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --------------------------------------------------------------------------- #
# Bootstrap and persistence
# --------------------------------------------------------------------------- #
def bootstrap(seed: int = 42, plan_now: bool = True) -> None:
    """Create tables, load the world (or seed it), and kick off a first plan."""
    global _STATE, _VERSION
    database.init_db()
    with _STATE_LOCK:
        with database.session_scope() as db:
            row = database.load_state_row(db)
            if row is None:
                state = WorldState(dataset=build_dataset(seed), seed=seed)
                _STATE = state
                _VERSION = 1
                database.save_state_row(db, state.model_dump_json(), _VERSION)
            else:
                _STATE = WorldState.model_validate_json(row.data)
                _VERSION = row.version
            _ensure_accounts(db, _STATE.dataset)
    if plan_now and _STATE is not None and _STATE.plan is None:
        replan()


def _ensure_accounts(db, dataset: Dataset) -> int:
    """Create any missing demo account. Idempotent, so restarts are cheap."""
    created = 0
    for spec in demo_accounts(dataset):
        if database.get_user(db, str(spec["username"])) is not None:
            continue
        create_user(
            db,
            username=str(spec["username"]),
            password=DEMO_PASSWORD,
            role=str(spec["role"]),
            ref_id=spec["ref_id"],  # type: ignore[arg-type]
            display_name=str(spec["display_name"]),
            village=spec["village"],  # type: ignore[arg-type]
            phone=spec["phone"],  # type: ignore[arg-type]
            lat=spec["lat"],  # type: ignore[arg-type]
            lng=spec["lng"],  # type: ignore[arg-type]
        )
        created += 1
    return created


#: Per-user inbox cap.
MAX_NOTIFICATIONS = 40


def _notify(state: WorldState, username: Optional[str], kind: str, title: str, body: str) -> None:
    """Drop a line in someone's bell. Silently ignores unknown recipients."""
    if not username:
        return
    inbox = state.notifications.setdefault(username, [])
    inbox.insert(
        0,
        Notification(
            id=uuid.uuid4().hex[:12],
            created_at=_now_iso(),
            kind=kind,
            title=title,
            body=body,
        ),
    )
    del inbox[MAX_NOTIFICATIONS:]


def _username_for(role: str, ref_id: Optional[str]) -> Optional[str]:
    """The account behind a truck or a buyer."""
    if not ref_id:
        return None
    with database.session_scope() as db:
        user = database.get_user_by_ref(db, role, ref_id)
        return user.username if user else None


def _fingerprint(route: Optional[RouteResult]) -> Optional[str]:
    """Identity of an *offer*.

    Two routes with the same fingerprint are the same deal for everyone on
    them: same buyer, same lots, same times, same money. If it changes, every
    acceptance gathered against the old one is void.
    """
    if route is None:
        return None
    parts = [route.truck_id, str(route.buyer_id), f"{route.cost:.2f}", str(route.distance_m)]
    for stop in route.stops:
        parts.append(f"{stop.kind.value}:{stop.ref_id}:{stop.arrival_min}:{stop.quantity_kg}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _crop_summary(route: RouteResult) -> str:
    """'tomato 783 kg + leafy greens 417 kg' for the buyer's confirmation."""
    totals: Counter = Counter()
    for stop in route.stops:
        if stop.kind == StopKind.FARMER and stop.crop:
            totals[stop.crop] += stop.quantity_kg
    return " + ".join(
        f"{crop.replace('_', ' ')} {kg:,} kg" for crop, kg in sorted(totals.items())
    )


def _require_state() -> WorldState:
    if _STATE is None:
        raise WorkflowError(503, "service is still starting up")
    return _STATE


def _persist(state: WorldState) -> int:
    global _VERSION
    _VERSION += 1
    with database.session_scope() as db:
        database.save_state_row(db, state.model_dump_json(), _VERSION)
    return _VERSION


@contextmanager
def mutate() -> Iterator[WorldState]:
    """Change the world, bump the version and save -- all under one lock."""
    with _STATE_LOCK:
        state = _require_state()
        yield state
        _persist(state)


@contextmanager
def read() -> Iterator[WorldState]:
    """Read the world without bumping the version."""
    with _STATE_LOCK:
        yield _require_state()


def version() -> int:
    with _STATE_LOCK:
        return _VERSION


def is_planning() -> bool:
    with _STATE_LOCK:
        return _planning_depth > 0 or _replan_queued


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _solver_input(state: WorldState) -> Tuple[Dataset, List[RouteResult], List[str]]:
    """Strip out everything an accepted trip already covers.

    Returns the reduced dataset to plan over, the locked routes to merge back,
    and the trucks sitting out this round because their driver declined.
    """
    dataset = state.dataset
    locked = [t for t in state.trips.values() if t.is_locked and t.route is not None]
    declined = sorted(
        tid for tid, trip in state.trips.items() if trip.status == TripStatus.DECLINED
    )

    locked_truck_ids = {trip.truck_id for trip in locked}
    locked_farmer_ids = {fid for trip in locked for fid in trip.farmer_ids()}

    trucks = [
        t for t in dataset.trucks
        if t.id not in locked_truck_ids and t.id not in set(declined)
    ]
    farmers = [f for f in dataset.active_farmers() if f.id not in locked_farmer_ids]

    committed: Counter = Counter()
    for trip in locked:
        route = trip.route
        if route is not None and route.buyer_id:
            committed[route.buyer_id] += route.load_kg

    buyers: List[Buyer] = []
    for buyer in dataset.buyers:
        remaining = buyer.demand_kg - committed.get(buyer.id, 0)
        if remaining <= 0:
            continue  # this buyer is already full from accepted trips
        buyers.append(
            buyer if remaining == buyer.demand_kg
            else buyer.model_copy(update={"demand_kg": remaining})
        )

    reduced = Dataset(
        crops=dataset.crops,
        farmers=farmers,
        buyers=buyers,
        trucks=trucks,
        plan_start_label=dataset.plan_start_label,
        seed=dataset.seed,
    )
    return reduced, [t.route for t in locked if t.route is not None], declined


def _merge_plan(
    dataset: Dataset,
    locked_routes: List[RouteResult],
    fresh: Optional[PlanResult],
    declined: List[str],
) -> PlanResult:
    """Locked routes plus the freshly planned ones, as one comparable plan."""
    routes = sorted(list(locked_routes) + (list(fresh.routes) if fresh else []),
                    key=lambda r: r.truck_id)
    served = {fid for route in routes for fid in route.farmer_ids}

    unserved: List[UnservedFarmer] = list(fresh.unserved) if fresh else []
    explained = {u.farmer_id for u in unserved}
    for farmer in dataset.active_farmers():
        if farmer.id in served or farmer.id in explained:
            continue
        unserved.append(
            UnservedFarmer(
                farmer_id=farmer.id,
                name=farmer.name,
                village=farmer.village,
                crop=farmer.crop,
                quantity_kg=farmer.quantity_kg,
                reason="no truck was available to plan a pickup for this lot",
                reason_code=UnservedReason.NO_TRUCK,
            )
        )

    notes = list(fresh.notes) if fresh else []
    if locked_routes:
        notes.insert(0, (
            f"{len(locked_routes)} accepted trip(s) were locked and left untouched: "
            + ", ".join(sorted(r.truck_id for r in locked_routes))
        ))
    if declined:
        notes.insert(0, f"Declined and sitting out this round: {', '.join(declined)}.")

    total_m = sum(r.distance_m for r in routes)
    return PlanResult(
        mode="optimized",
        routes=routes,
        unserved=unserved,
        total_distance_m=total_m,
        total_distance_km=round(total_m / 1000.0, 2),
        total_duration_min=sum(r.duration_min for r in routes),
        total_cost=round(sum(r.cost for r in routes), 2),
        farmers_served=sum(len(r.farmer_ids) for r in routes),
        kg_moved=sum(r.load_kg for r in routes),
        trucks_used=len(routes),
        solver_status=fresh.solver_status if fresh else "LOCKED_ONLY",
        solve_seconds=fresh.solve_seconds if fresh else 0.0,
        notes=notes,
    )


def _refresh_status(state: WorldState, trip: Trip) -> None:
    """Derive a trip's status, and announce the moment it becomes binding."""
    if trip.driver_declined:
        trip.status = TripStatus.DECLINED
        return
    if trip.route is None:
        trip.status = TripStatus.IDLE
        return
    if trip.status in (TripStatus.IN_TRANSIT, TripStatus.DELIVERED):
        return

    if trip.driver_accepted and trip.all_farmers_accepted():
        if trip.status != TripStatus.CONFIRMED:
            trip.status = TripStatus.CONFIRMED
            trip.confirmed_at = _now_iso()
            _announce_confirmation(state, trip)
    else:
        trip.status = TripStatus.PROPOSED


def _announce_confirmation(state: WorldState, trip: Trip) -> None:
    """Tell the buyer, the driver and the farmers that the deal is on."""
    route = trip.route
    if route is None:
        return
    eta = minutes_to_clock(route.delivery_min)
    crops = _crop_summary(route)

    buyer_user = _username_for(Role.BUYER.value, route.buyer_id)
    _notify(
        state, buyer_user, "confirmed",
        "Order confirmed",
        f"Order confirmed \u2014 {trip.truck_id} on the way, ETA {eta}, {crops}.",
    )

    driver_user = _username_for(Role.DRIVER.value, trip.truck_id)
    _notify(
        state, driver_user, "confirmed",
        f"Trip {trip.truck_id} confirmed",
        f"Every farmer has accepted. {len(route.farmer_ids)} pickups, "
        f"deliver to {route.buyer_name} by {eta}.",
    )

    for farmer_id in route.farmer_ids:
        farmer = state.dataset.farmer_by_id(farmer_id)
        if farmer is None or farmer.pre_accepted:
            continue
        stop = _stop_for_farmer(route, farmer_id)
        when = stop.arrival_clock if stop else eta
        _notify(
            state, farmer.owner, "confirmed",
            "Pickup confirmed",
            f"{trip.truck_id} will collect your {farmer.crop.replace('_', ' ')} "
            f"at about {when}, for {route.buyer_name}.",
        )


def _reconcile_trips(state: WorldState, fresh_routes: List[RouteResult]) -> None:
    """Rebuild every unconfirmed trip from the new plan.

    Confirmed trips are untouchable. For the rest, an offer whose shape changed
    voids the acceptances gathered against the old one -- and says so, because
    silently moving a trip somebody already agreed to is how a demo loses
    trust. Seeded lots re-accept themselves so only live farmers have to tap.
    """
    now = _now_iso()
    by_truck = {route.truck_id: route for route in fresh_routes}
    before = {
        truck_id: (trip.fingerprint, trip.driver_accepted, dict(trip.farmer_status))
        for truck_id, trip in state.trips.items()
    }
    previously_matched = {
        farmer_id
        for trip in state.trips.values()
        for farmer_id in trip.farmer_ids()
    }

    for truck in state.dataset.trucks:
        trip = state.trips.get(truck.id)
        if trip is not None and trip.is_locked:
            continue
        if trip is not None and trip.driver_declined:
            trip.route = None
            trip.farmer_status = {}
            trip.stop_status = {}
            trip.fee = 0.0
            trip.fingerprint = None
            trip.status = TripStatus.DECLINED
            continue

        route = by_truck.get(truck.id)
        previous_fp, driver_was_in, farmers_were = before.get(truck.id, (None, False, {}))

        if route is None:
            state.trips[truck.id] = Trip(truck_id=truck.id, status=TripStatus.IDLE)
            continue

        fingerprint = _fingerprint(route)
        unchanged = fingerprint == previous_fp

        farmer_status: Dict[str, AcceptStatus] = {}
        for farmer_id in route.farmer_ids:
            farmer = state.dataset.farmer_by_id(farmer_id)
            if farmer is not None and farmer.pre_accepted:
                farmer_status[farmer_id] = AcceptStatus.ACCEPTED
            elif unchanged and farmer_id in farmers_were:
                farmer_status[farmer_id] = farmers_were[farmer_id]
            else:
                farmer_status[farmer_id] = AcceptStatus.PENDING

        state.trips[truck.id] = Trip(
            truck_id=truck.id,
            status=TripStatus.PROPOSED,
            route=route,
            driver_accepted=driver_was_in if unchanged else False,
            farmer_status=farmer_status,
            stop_status={fid: StopStatus.PENDING for fid in route.farmer_ids},
            fee=round(route.cost, 2),
            fingerprint=fingerprint,
            proposed_at=now,
        )

        new_trip = state.trips[truck.id]
        if previous_fp is None:
            _notify(
                state, _username_for(Role.DRIVER.value, truck.id), "trip_offer",
                "New trip offer",
                f"{len(route.farmer_ids)} pickups, deliver to {route.buyer_name} by "
                f"{minutes_to_clock(route.delivery_min)}. Fee \u20b9{route.cost:,.0f}.",
            )
        for farmer_id, accept in new_trip.farmer_status.items():
            farmer = state.dataset.farmer_by_id(farmer_id)
            if farmer is None or farmer.pre_accepted or accept != AcceptStatus.PENDING:
                continue
            if unchanged and farmers_were.get(farmer_id) == AcceptStatus.PENDING:
                continue  # same offer they have already been told about
            stop = _stop_for_farmer(route, farmer_id)
            price = None
            buyer = state.dataset.buyer_by_id(route.buyer_id) if route.buyer_id else None
            if buyer is not None:
                price = buyer.price_for(farmer.crop)
            _notify(
                state, farmer.owner, "match_offer",
                "Match found \u2014 accept?",
                f"{route.buyer_name} will take your {farmer.crop.replace('_', ' ')}"
                + (f" at \u20b9{price:.2f}/kg" if price is not None else "")
                + f". Truck {truck.id} arrives ~{stop.arrival_clock if stop else '?'}.",
            )

        if not unchanged and previous_fp is not None:
            if driver_was_in:
                _notify(
                    state, _username_for(Role.DRIVER.value, truck.id), "changed",
                    "Your trip changed",
                    "Your trip changed \u2014 please review.",
                )
            for farmer_id, was in farmers_were.items():
                farmer = state.dataset.farmer_by_id(farmer_id)
                if was != AcceptStatus.ACCEPTED or farmer is None or farmer.pre_accepted:
                    continue
                _notify(
                    state, farmer.owner, "changed",
                    "Your pickup changed",
                    "Your trip changed \u2014 please review.",
                )

    for truck_id in [t for t in state.trips if state.dataset.truck_by_id(t) is None]:
        del state.trips[truck_id]

    for trip in state.trips.values():
        _refresh_status(state, trip)

    # A live lot that nobody will pay for: say so once, with the best offer.
    for entry in (state.plan.unserved if state.plan else []):
        if entry.reason_code != UnservedReason.PRICE:
            continue
        farmer = state.dataset.farmer_by_id(entry.farmer_id)
        if farmer is None or farmer.pre_accepted or farmer.withdrawn:
            continue
        inbox = state.notifications.get(farmer.owner or "", [])
        if inbox and inbox[0].kind == "price" and inbox[0].body == entry.reason:
            continue
        _notify(state, farmer.owner, "price", "No buyer at your price", entry.reason)

    # Someone who had a match and lost it deserves to hear it from us.
    still_matched = {fid for trip in state.trips.values() for fid in trip.farmer_ids()}
    for farmer_id in sorted(previously_matched - still_matched):
        farmer = state.dataset.farmer_by_id(farmer_id)
        if farmer is None or farmer.pre_accepted or farmer.withdrawn:
            continue
        _notify(
            state, farmer.owner, "changed",
            "No truck for your lot",
            f"Your {farmer.crop.replace('_', ' ')} is not on a truck in the latest plan.",
        )


def _run_replan(time_limit: Optional[float] = None) -> None:
    global _planning_depth, _replan_queued
    with _PLAN_LOCK:
        with _STATE_LOCK:
            _replan_queued = False
            _planning_depth += 1
        try:
            with _STATE_LOCK:
                state = _require_state()
                reduced, locked_routes, declined = _solver_input(state)
                full_dataset = state.dataset.model_copy(deep=True)

            # The expensive part runs outside the state lock so every poll in
            # flight keeps answering.
            budget = PLAN_TIME_LIMIT if time_limit is None else time_limit
            fresh = solve(reduced, time_limit_seconds=budget) if reduced.farmers else None
            greedy = baseline_planner.plan(full_dataset)

            with _STATE_LOCK:
                state = _require_state()
                state.plan = _merge_plan(state.dataset, locked_routes, fresh, declined)
                state.baseline = greedy
                state.last_planned_at = _now_iso()
                _reconcile_trips(state, list(fresh.routes) if fresh else [])
                _persist(state)
        finally:
            with _STATE_LOCK:
                _planning_depth -= 1


def replan(blocking: bool = False, time_limit: Optional[float] = None) -> None:
    """Re-plan around the locked trips.

    Non-blocking by default: the caller's request returns at once and the views
    show "Re-optimising..." until the new plan lands. At most one run is queued
    behind the one in flight, because a queued run reads the latest state
    anyway, so three quick lot postings cost two solves, not three.
    """
    global _replan_queued
    if blocking:
        _run_replan(time_limit)
        return
    with _STATE_LOCK:
        if _replan_queued:
            return
        _replan_queued = True
    threading.Thread(
        target=_run_replan, args=(time_limit,), name="kisanroute-replan", daemon=True
    ).start()


def wait_for_plan(timeout: float = 60.0) -> bool:
    """Block until no re-plan is running or queued. Used by tests and startup."""
    deadline = datetime.now(timezone.utc).timestamp() + timeout
    while datetime.now(timezone.utc).timestamp() < deadline:
        if not is_planning():
            return True
        threading.Event().wait(0.05)
    return False


# --------------------------------------------------------------------------- #
# Lookups shared by the views
# --------------------------------------------------------------------------- #
def _next_lot_id(dataset: Dataset) -> str:
    n = 1
    used = {f.id for f in dataset.farmers}
    while f"F{n:02d}" in used:
        n += 1
    return f"F{n:02d}"


def _trip_for_farmer(state: WorldState, farmer_id: str) -> Optional[Trip]:
    for trip in state.trips.values():
        if trip.route is not None and farmer_id in trip.route.farmer_ids:
            return trip
    return None


def _stop_for_farmer(route: RouteResult, farmer_id: str):
    for stop in route.stops:
        if stop.kind == StopKind.FARMER and stop.ref_id == farmer_id:
            return stop
    return None


def _unserved_entry(state: WorldState, farmer_id: str) -> Optional[UnservedFarmer]:
    if state.plan is None:
        return None
    for entry in state.plan.unserved:
        if entry.farmer_id == farmer_id:
            return entry
    return None


def _payout_index(state: WorldState) -> Dict[str, object]:
    """farmer id -> payout row for the current plan. Never handed out wholesale."""
    if state.plan is None:
        return {}
    summary = metrics.payouts(state.dataset, state.plan)
    return {row.farmer_id: row for row in summary.farmers}


def _lot_status(farmer: Farmer, trip: Optional[Trip], unmatched: bool) -> LotStatus:
    if farmer.withdrawn:
        return LotStatus.WITHDRAWN
    if trip is None or trip.route is None:
        return LotStatus.UNMATCHED if unmatched else LotStatus.WAITING
    if trip.status == TripStatus.DELIVERED:
        return LotStatus.DELIVERED
    if trip.stop_status.get(farmer.id) == StopStatus.PICKED_UP:
        return LotStatus.PICKED_UP
    if trip.status in (TripStatus.CONFIRMED, TripStatus.IN_TRANSIT):
        return LotStatus.CONFIRMED
    mine = trip.farmer_status.get(farmer.id, AcceptStatus.PENDING)
    if mine == AcceptStatus.DECLINED:
        return LotStatus.DECLINED
    if mine == AcceptStatus.ACCEPTED:
        return LotStatus.ACCEPTED
    return LotStatus.MATCHED


def _lot_card(state: WorldState, farmer: Farmer, payouts: Dict[str, object]) -> Dict[str, object]:
    """Everything a farmer may see about one of their own lots."""
    trip = _trip_for_farmer(state, farmer.id)
    entry = _unserved_entry(state, farmer.id)
    status = _lot_status(farmer, trip, entry is not None)
    crop = state.dataset.crops.get(farmer.crop)

    card: Dict[str, object] = {
        "lot_id": farmer.id,
        "crop": farmer.crop,
        "quantity_kg": farmer.quantity_kg,
        "min_price_per_kg": farmer.min_price_per_kg,
        "harvest_time": farmer.harvest_time,
        "ready_from": farmer.ready_from,
        "ready_from_clock": minutes_to_clock(farmer.ready_from),
        "ready_until": farmer.ready_until,
        "ready_until_clock": minutes_to_clock(farmer.ready_until),
        "spoilage_deadline_min": state.dataset.spoilage_deadline(farmer),
        "spoilage_deadline_clock": minutes_to_clock(state.dataset.spoilage_deadline(farmer)),
        "spoilage_hours": crop.spoilage_hours if crop else None,
        "status": status.value,
        "withdrawn": farmer.withdrawn,
        "reason": entry.reason if entry else None,
        "reason_code": entry.reason_code.value if entry else None,
        "best_offer_per_kg": (
            entry.best_offer_per_kg if entry else state.dataset.best_offer_for_crop(farmer.crop)
        ),
        "my_acceptance": None,
        "truck_id": None,
        "trip_status": None,
        "awaiting_farmers": 0,
        "driver_accepted": False,
        "eta_min": None,
        "eta_clock": None,
        "buyer_id": None,
        "buyer_name": None,
        "price_per_kg": None,
        "gross_revenue": None,
        "transport_share": None,
        "net_payout": None,
        "middleman_price_per_kg": crop.middleman_price_per_kg if crop else None,
        "middleman_revenue": None,
        "gain_vs_middleman": None,
        "can_accept": False,
        "can_edit_price": not farmer.withdrawn,
    }

    if trip is not None and trip.route is not None:
        stop = _stop_for_farmer(trip.route, farmer.id)
        mine = trip.farmer_status.get(farmer.id, AcceptStatus.PENDING)
        card.update(
            truck_id=trip.truck_id,
            trip_status=trip.status.value,
            my_acceptance=mine.value,
            driver_accepted=trip.driver_accepted,
            awaiting_farmers=len(trip.awaiting_farmers()),
            buyer_id=trip.route.buyer_id,
            buyer_name=trip.route.buyer_name,
            can_accept=(mine != AcceptStatus.ACCEPTED and not trip.is_locked),
            can_edit_price=not trip.is_locked,
        )
        if stop is not None:
            card["eta_min"] = stop.arrival_min
            card["eta_clock"] = stop.arrival_clock
        row = payouts.get(farmer.id)
        if row is not None:
            card.update(
                price_per_kg=row.price_per_kg,
                gross_revenue=row.gross_revenue,
                transport_share=row.transport_share,
                net_payout=row.net_payout,
                middleman_price_per_kg=row.middleman_price_per_kg,
                middleman_revenue=row.middleman_revenue,
                gain_vs_middleman=row.gain_vs_middleman,
            )
    return card


def _anonymised_route(route: RouteResult, farmer_id: str) -> List[Dict[str, object]]:
    """The route geometry a farmer may see.

    Their truck's shape, timings and their own stop are all theirs to see. The
    other growers on the same truck are reduced to "Pickup 3" -- no name, no
    quantity and no payout, because the brief is explicit that a farmer must
    never see another farmer's business.
    """
    out: List[Dict[str, object]] = []
    pickup_index = 0
    for stop in route.stops:
        mine = stop.kind == StopKind.FARMER and stop.ref_id == farmer_id
        if stop.kind == StopKind.FARMER:
            pickup_index += 1
        out.append(
            {
                "seq": stop.seq,
                "kind": stop.kind.value,
                "lat": stop.lat,
                "lng": stop.lng,
                "arrival_min": stop.arrival_min,
                "arrival_clock": stop.arrival_clock,
                "mine": mine,
                "pickup_index": pickup_index if stop.kind == StopKind.FARMER else None,
                "name": stop.name if (mine or stop.kind != StopKind.FARMER) else None,
                "quantity_kg": stop.quantity_kg if mine else None,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Role-scoped views. Access control lives here, not in the frontend.
# --------------------------------------------------------------------------- #
def farmer_view(state: WorldState, username: str) -> Dict[str, object]:
    lots = [f for f in state.dataset.farmers if f.owner == username]
    payouts = _payout_index(state)
    cards = [_lot_card(state, farmer, payouts) for farmer in lots]

    # Lead with whatever needs the farmer's attention, then with live deals.
    priority = [
        LotStatus.MATCHED.value, LotStatus.UNMATCHED.value, LotStatus.ACCEPTED.value,
        LotStatus.CONFIRMED.value, LotStatus.PICKED_UP.value, LotStatus.DELIVERED.value,
        LotStatus.WAITING.value,
    ]
    active = None
    for wanted in priority:
        active = next((c for c in cards if c["status"] == wanted), None)
        if active is not None:
            break
    if active is None and cards:
        active = cards[0]

    route: List[Dict[str, object]] = []
    truck = None
    if active and active.get("truck_id"):
        trip = state.trips.get(str(active["truck_id"]))
        if trip is not None and trip.route is not None:
            route = _anonymised_route(trip.route, str(active["lot_id"]))
            truck_row = state.dataset.truck_by_id(trip.truck_id)
            truck = {
                "truck_id": trip.truck_id,
                "status": trip.status.value,
                "capacity_kg": truck_row.capacity_kg if truck_row else None,
                "colour_key": trip.truck_id,
            }

    buyers_for_map = []
    if active and active.get("buyer_id"):
        buyer = state.dataset.buyer_by_id(str(active["buyer_id"]))
        if buyer is not None:
            buyers_for_map.append(
                {"id": buyer.id, "name": buyer.name, "lat": buyer.lat, "lng": buyer.lng}
            )

    return {
        "lots": cards,
        "active_lot_id": active["lot_id"] if active else None,
        "route": route,
        "truck": truck,
        "buyers": buyers_for_map,
        "crops": {
            name: {"spoilage_hours": crop.spoilage_hours,
                   "base_price_per_kg": crop.base_price_per_kg,
                   "middleman_price_per_kg": crop.middleman_price_per_kg,
                   # The best price on the board today -- shown as a hint when
                   # the farmer sets a minimum, so nobody prices blind.
                   "best_offer_per_kg": state.dataset.best_offer_for_crop(name)}
            for name, crop in state.dataset.crops.items()
        },
        "totals": {
            "lots": len(cards),
            "kg": sum(int(c["quantity_kg"]) for c in cards),
            "net_payout": round(sum(float(c["net_payout"] or 0) for c in cards), 2),
            "middleman": round(sum(float(c["middleman_revenue"] or 0) for c in cards), 2),
        },
    }


def driver_view(state: WorldState, truck_id: str) -> Dict[str, object]:
    trip = state.trips.get(truck_id)
    truck = state.dataset.truck_by_id(truck_id)
    if trip is None or trip.route is None:
        return {
            "trip": None,
            "truck": {"truck_id": truck_id,
                      "capacity_kg": truck.capacity_kg if truck else None,
                      "depot": {"lat": truck.depot_lat, "lng": truck.depot_lng} if truck else None},
            "status": trip.status.value if trip else TripStatus.IDLE.value,
        }

    route = trip.route
    stops: List[Dict[str, object]] = []
    for stop in route.stops:
        entry: Dict[str, object] = {
            "seq": stop.seq,
            "kind": stop.kind.value,
            "ref_id": stop.ref_id,
            "name": stop.name,
            "lat": stop.lat,
            "lng": stop.lng,
            "arrival_min": stop.arrival_min,
            "arrival_clock": stop.arrival_clock,
            "quantity_kg": stop.quantity_kg,
            "crop": stop.crop,
            "village": stop.village,
            "load_kg_after": stop.load_kg_after,
        }
        if stop.kind == StopKind.FARMER:
            farmer = state.dataset.farmer_by_id(stop.ref_id)
            accepted = trip.farmer_status.get(stop.ref_id, AcceptStatus.PENDING)
            entry["phone"] = farmer.phone if farmer else None
            entry["status"] = trip.stop_status.get(stop.ref_id, StopStatus.PENDING).value
            entry["acceptance"] = accepted.value
            entry["badge"] = (
                "farmer confirmed" if accepted == AcceptStatus.ACCEPTED
                else "farmer declined" if accepted == AcceptStatus.DECLINED
                else "awaiting farmer"
            )
        stops.append(entry)

    return {
        "trip": {
            "truck_id": trip.truck_id,
            "status": trip.status.value,
            "buyer_id": route.buyer_id,
            "buyer_name": route.buyer_name,
            "stops": stops,
            "pickups": len(route.farmer_ids),
            "picked_up": sum(
                1 for v in trip.stop_status.values() if v == StopStatus.PICKED_UP
            ),
            "load_kg": route.load_kg,
            "capacity_kg": route.truck_capacity_kg,
            "utilisation_pct": route.utilisation_pct,
            "distance_km": route.distance_km,
            "duration_min": route.duration_min,
            "fee": trip.fee,
            "delivery_min": route.delivery_min,
            "delivery_clock": minutes_to_clock(route.delivery_min),
            "start_clock": minutes_to_clock(route.start_min),
            "driver_accepted": trip.driver_accepted,
            "awaiting_farmers": len(trip.awaiting_farmers()),
            "farmers_accepted": sum(
                1 for v in trip.farmer_status.values() if v == AcceptStatus.ACCEPTED
            ),
            "can_accept": trip.status == TripStatus.PROPOSED and not trip.driver_accepted,
            "can_decline": trip.status == TripStatus.PROPOSED,
            "proposed_at": trip.proposed_at,
            "accepted_at": trip.accepted_at,
            "confirmed_at": trip.confirmed_at,
            "delivered_at": trip.delivered_at,
        },
        "truck": {
            "truck_id": truck_id,
            "capacity_kg": truck.capacity_kg if truck else route.truck_capacity_kg,
            "cost_per_km": truck.cost_per_km if truck else None,
            "depot": {"lat": truck.depot_lat, "lng": truck.depot_lng} if truck else None,
        },
        "status": trip.status.value,
    }


def buyer_view(state: WorldState, buyer_id: str) -> Dict[str, object]:
    buyer = state.dataset.buyer_by_id(buyer_id)
    incoming: List[Dict[str, object]] = []

    for trip in sorted(state.trips.values(), key=lambda t: t.truck_id):
        route = trip.route
        if route is None or route.buyer_id != buyer_id:
            continue
        crops: Counter = Counter()
        for stop in route.stops:
            if stop.kind == StopKind.FARMER and stop.crop:
                crops[stop.crop] += stop.quantity_kg
        incoming.append(
            {
                "truck_id": trip.truck_id,
                "status": trip.status.value,
                "eta_min": route.delivery_min,
                "eta_clock": minutes_to_clock(route.delivery_min),
                "load_kg": route.load_kg,
                "lots": len(route.farmer_ids),
                "crops": dict(sorted(crops.items())),
                "picked_up": sum(1 for v in trip.stop_status.values() if v == StopStatus.PICKED_UP),
                "pickups": len(route.farmer_ids),
                "confirmed": trip.status in (
                    TripStatus.CONFIRMED, TripStatus.IN_TRANSIT, TripStatus.DELIVERED
                ),
                "awaiting_farmers": len(trip.awaiting_farmers()),
                "driver_accepted": trip.driver_accepted,
                "crops_text": _crop_summary(route),
                "on_time": route.delivery_on_time,
                "route": [
                    {"lat": s.lat, "lng": s.lng, "kind": s.kind.value,
                     "arrival_clock": s.arrival_clock}
                    for s in route.stops
                ],
                "value": round(
                    sum(
                        stop.quantity_kg * (buyer.price_for(stop.crop or "") if buyer else 0.0)
                        for stop in route.stops
                        if stop.kind == StopKind.FARMER
                    ),
                    2,
                ),
            }
        )

    committed = sum(int(entry["load_kg"]) for entry in incoming)
    confirmed_kg = sum(int(e["load_kg"]) for e in incoming if e["confirmed"])
    return {
        "buyer": buyer.model_dump() if buyer else None,
        "incoming": incoming,
        "pending": [e for e in incoming if not e["confirmed"]],
        "confirmed": [e for e in incoming if e["confirmed"]],
        "committed_kg": committed,
        "confirmed_kg": confirmed_kg,
        "prices": dict(buyer.price_per_kg) if buyer else {},
        "outstanding_kg": max(0, (buyer.demand_kg if buyer else 0) - committed),
        "crops": sorted(state.dataset.crops),
    }


def trip_summaries(state: WorldState) -> List[Dict[str, object]]:
    """The coordinator's live board: every truck, whatever its status."""
    rows: List[Dict[str, object]] = []
    for truck in state.dataset.trucks:
        trip = state.trips.get(truck.id)
        route = trip.route if trip else None
        rows.append(
            {
                "truck_id": truck.id,
                "capacity_kg": truck.capacity_kg,
                "status": trip.status.value if trip else TripStatus.IDLE.value,
                "buyer_id": route.buyer_id if route else None,
                "buyer_name": route.buyer_name if route else None,
                "pickups": len(route.farmer_ids) if route else 0,
                "picked_up": sum(
                    1 for v in trip.stop_status.values() if v == StopStatus.PICKED_UP
                ) if trip else 0,
                "load_kg": route.load_kg if route else 0,
                "distance_km": route.distance_km if route else 0.0,
                "fee": trip.fee if trip else 0.0,
                "delivery_clock": minutes_to_clock(route.delivery_min) if route else None,
                "accepted_at": trip.accepted_at if trip else None,
                "delivered_at": trip.delivered_at if trip else None,
                "driver_accepted": trip.driver_accepted if trip else False,
                "awaiting_farmers": len(trip.awaiting_farmers()) if trip else 0,
                "farmers_accepted": sum(
                    1 for v in trip.farmer_status.values() if v == AcceptStatus.ACCEPTED
                ) if trip else 0,
            }
        )
    return rows


def unmatched_lots(state: WorldState) -> List[Dict[str, object]]:
    """Every lot no truck is coming for, and why -- the coordinator's worklist."""
    rows: List[Dict[str, object]] = []
    for entry in (state.plan.unserved if state.plan else []):
        farmer = state.dataset.farmer_by_id(entry.farmer_id)
        rows.append(
            {
                "lot_id": entry.farmer_id,
                "name": entry.name,
                "village": entry.village,
                "crop": entry.crop,
                "quantity_kg": entry.quantity_kg,
                "reason": entry.reason,
                "reason_code": entry.reason_code.value,
                "min_price_per_kg": (
                    entry.min_price_per_kg
                    if entry.min_price_per_kg is not None
                    else (farmer.min_price_per_kg if farmer else None)
                ),
                "best_offer_per_kg": (
                    entry.best_offer_per_kg
                    if entry.best_offer_per_kg is not None
                    else state.dataset.best_offer_for_crop(entry.crop)
                ),
                "owner": farmer.owner if farmer else None,
            }
        )
    rows.sort(key=lambda r: (r["reason_code"] != "price", r["lot_id"]))
    return rows


def coordinator_view(state: WorldState) -> Dict[str, object]:
    plan = state.plan
    unmatched = unmatched_lots(state)
    return {
        "trips": trip_summaries(state),
        "unmatched": unmatched,
        "unmatched_by_reason": {
            code: sum(1 for r in unmatched if r["reason_code"] == code)
            for code in sorted({str(r["reason_code"]) for r in unmatched})
        },
        "summary": state.dataset.summary(),
        "plan": {
            "farmers_served": plan.farmers_served if plan else 0,
            "kg_moved": plan.kg_moved if plan else 0,
            "total_distance_km": plan.total_distance_km if plan else 0.0,
            "total_cost": plan.total_cost if plan else 0.0,
            "unserved": len(plan.unserved) if plan else 0,
            "solver_status": plan.solver_status if plan else "NOT_PLANNED",
            "last_planned_at": state.last_planned_at,
        },
        "accounts": {"disruptions": list(state.disruptions)},
    }


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def post_lot(user: UserPublic, payload) -> Farmer:
    """A farmer lists produce. Always triggers a re-plan."""
    if user.role != Role.FARMER:
        raise WorkflowError(403, "only a farmer account can post produce")
    if user.lat is None or user.lng is None:
        raise WorkflowError(400, "this account has no farm location on file")

    with mutate() as state:
        if payload.crop not in state.dataset.crops:
            raise WorkflowError(
                400, f"unknown crop '{payload.crop}'; known crops: {sorted(state.dataset.crops)}"
            )
        harvest_time = payload.harvest_time if payload.harvest_time is not None else 0
        ready_from = (
            payload.ready_from if payload.ready_from is not None
            else harvest_time + DEFAULT_PACK_TIME_MIN
        )
        ready_until = (
            payload.ready_until if payload.ready_until is not None
            else ready_from + DEFAULT_PICKUP_WINDOW_MIN
        )
        if ready_until < ready_from:
            raise WorkflowError(
                400, f"ready_until ({ready_until}) precedes ready_from ({ready_from})"
            )

        lot = Farmer(
            id=_next_lot_id(state.dataset),
            name=user.display_name,
            village=user.village or "Unassigned",
            lat=user.lat,
            lng=user.lng,
            crop=payload.crop,
            quantity_kg=payload.quantity_kg,
            min_price_per_kg=payload.min_price_per_kg,
            harvest_time=harvest_time,
            ready_from=ready_from,
            ready_until=ready_until,
            phone=user.phone,
            # A live farmer answers for themselves; only the seed pre-accepts.
            pre_accepted=False,
            owner=user.username,
        )
        state.dataset.farmers.append(lot)

    replan()
    return lot


def set_demand(user: UserPublic, payload) -> Buyer:
    """A buyer updates what it wants today. Triggers a re-plan."""
    if user.role != Role.BUYER or not user.ref_id:
        raise WorkflowError(403, "only a buyer account can post demand")

    with mutate() as state:
        buyer = state.dataset.buyer_by_id(user.ref_id)
        if buyer is None:
            raise WorkflowError(404, f"buyer '{user.ref_id}' is no longer in the dataset")

        if payload.crops_wanted is not None:
            unknown = [c for c in payload.crops_wanted if c not in state.dataset.crops]
            if unknown:
                raise WorkflowError(400, f"unknown crops {unknown}")
            buyer.crops_wanted = list(payload.crops_wanted)
        if payload.demand_kg is not None:
            buyer.demand_kg = payload.demand_kg
        if payload.window_start is not None:
            buyer.window_start = payload.window_start
        if payload.window_end is not None:
            buyer.window_end = payload.window_end
        if buyer.window_end <= buyer.window_start:
            raise WorkflowError(
                400,
                f"window_end ({buyer.window_end}) must exceed window_start ({buyer.window_start})",
            )

        from .seed import default_price_for  # local import keeps the module graph flat

        if isinstance(payload.price_per_kg, dict):
            buyer.price_per_kg.update({k: float(v) for k, v in payload.price_per_kg.items()})
        elif payload.price_per_kg is not None:
            buyer.price_per_kg = {c: float(payload.price_per_kg) for c in buyer.crops_wanted}
        for crop_name in buyer.crops_wanted:
            buyer.price_per_kg.setdefault(
                crop_name, default_price_for(state.dataset.crops[crop_name], buyer.type)
            )
        result = buyer.model_copy(deep=True)

    replan()
    return result


def _trip_or_404(state: WorldState, truck_id: str) -> Trip:
    trip = state.trips.get(truck_id)
    if trip is None:
        raise WorkflowError(404, f"no trip for truck '{truck_id}'")
    return trip


def _require_driver(user: UserPublic, truck_id: str) -> None:
    if user.role == Role.COORDINATOR:
        return
    if user.role == Role.DRIVER and user.ref_id == truck_id:
        return
    raise WorkflowError(403, f"account '{user.username}' does not drive truck '{truck_id}'")


def accept_trip(user: UserPublic, truck_id: str) -> Trip:
    """The driver takes the offer.

    This alone does not lock anything: the trip is binding only once every
    farmer on it has accepted too, which :func:`_refresh_status` decides.
    """
    _require_driver(user, truck_id)
    with mutate() as state:
        trip = _trip_or_404(state, truck_id)
        if trip.status != TripStatus.PROPOSED:
            raise WorkflowError(409, f"trip {truck_id} is '{trip.status.value}', not 'proposed'")
        if trip.route is None:
            raise WorkflowError(409, f"trip {truck_id} has no route to accept")
        trip.driver_accepted = True
        trip.accepted_at = _now_iso()
        _refresh_status(state, trip)
        return trip.model_copy(deep=True)


def decline_trip(user: UserPublic, truck_id: str) -> Trip:
    """Drop this truck out of the round and re-plan what it was carrying."""
    _require_driver(user, truck_id)
    with mutate() as state:
        trip = _trip_or_404(state, truck_id)
        if trip.is_locked:
            raise WorkflowError(409, f"trip {truck_id} is already {trip.status.value}")
        trip.status = TripStatus.DECLINED
        trip.driver_declined = True
        trip.driver_accepted = False
        trip.declined_at = _now_iso()
        trip.route = None
        trip.stop_status = {}
        trip.farmer_status = {}
        trip.fingerprint = None
        trip.fee = 0.0
        snapshot = trip.model_copy(deep=True)

    replan()
    return snapshot


def mark_picked_up(user: UserPublic, truck_id: str, farmer_id: str) -> Trip:
    _require_driver(user, truck_id)
    with mutate() as state:
        trip = _trip_or_404(state, truck_id)
        if trip.status not in (TripStatus.CONFIRMED, TripStatus.IN_TRANSIT):
            raise WorkflowError(
                409, f"trip {truck_id} must be confirmed before stops can be collected"
            )
        if farmer_id not in trip.stop_status:
            raise WorkflowError(404, f"lot '{farmer_id}' is not a stop on trip {truck_id}")
        trip.stop_status[farmer_id] = StopStatus.PICKED_UP
        if trip.status == TripStatus.CONFIRMED:
            trip.status = TripStatus.IN_TRANSIT
        return trip.model_copy(deep=True)


def mark_delivered(user: UserPublic, truck_id: str) -> Trip:
    _require_driver(user, truck_id)
    with mutate() as state:
        trip = _trip_or_404(state, truck_id)
        if trip.status not in (TripStatus.CONFIRMED, TripStatus.IN_TRANSIT):
            raise WorkflowError(409, f"trip {truck_id} is '{trip.status.value}', not under way")
        trip.status = TripStatus.DELIVERED
        trip.delivered_at = _now_iso()
        return trip.model_copy(deep=True)


def _own_lot_or_403(state: WorldState, user: UserPublic, lot_id: str) -> Farmer:
    lot = state.dataset.farmer_by_id(lot_id)
    if lot is None:
        raise WorkflowError(404, f"no lot '{lot_id}'")
    if user.role == Role.COORDINATOR:
        return lot
    if user.role != Role.FARMER or lot.owner != user.username:
        raise WorkflowError(403, f"lot '{lot_id}' does not belong to '{user.username}'")
    return lot


def accept_match(user: UserPublic, lot_id: str) -> Dict[str, object]:
    """The farmer takes the offered match. May be the last yes a trip needs."""
    with mutate() as state:
        lot = _own_lot_or_403(state, user, lot_id)
        trip = _trip_for_farmer(state, lot.id)
        if trip is None or trip.route is None:
            raise WorkflowError(409, f"lot '{lot_id}' has no match to accept")
        if trip.is_locked:
            raise WorkflowError(409, f"trip {trip.truck_id} is already {trip.status.value}")
        trip.farmer_status[lot.id] = AcceptStatus.ACCEPTED
        _refresh_status(state, trip)
        return {"lot_id": lot.id, "truck_id": trip.truck_id, "trip_status": trip.status.value}


def decline_match(user: UserPublic, lot_id: str) -> Dict[str, object]:
    """The farmer turns the match down, which withdraws the lot and re-plans."""
    with mutate() as state:
        lot = _own_lot_or_403(state, user, lot_id)
        trip = _trip_for_farmer(state, lot.id)
        if trip is not None and trip.is_locked:
            raise WorkflowError(
                409, f"trip {trip.truck_id} is already {trip.status.value} and cannot be declined"
            )
        if trip is not None:
            trip.farmer_status[lot.id] = AcceptStatus.DECLINED
        lot.withdrawn = True
        truck_id = trip.truck_id if trip else None
        _notify(
            state, lot.owner, "info", "Lot withdrawn",
            f"You turned down the match for your {lot.crop.replace('_', ' ')}. "
            "Post it again, or change the price, whenever you like.",
        )

    replan()
    return {"lot_id": lot_id, "withdrawn": True, "was_on": truck_id}


def set_lot_price(user: UserPublic, lot_id: str, min_price_per_kg: float) -> Farmer:
    """The Edit price button. A lower floor can open up buyers, so re-plan."""
    with mutate() as state:
        lot = _own_lot_or_403(state, user, lot_id)
        trip = _trip_for_farmer(state, lot.id)
        if trip is not None and trip.is_locked:
            raise WorkflowError(
                409, f"lot '{lot_id}' is on confirmed trip {trip.truck_id}; price is settled"
            )
        lot.min_price_per_kg = float(min_price_per_kg)
        # Re-posting a price is also a fresh offer: any acceptance against the
        # old price no longer means anything.
        lot.withdrawn = False
        if trip is not None:
            trip.farmer_status[lot.id] = AcceptStatus.PENDING
        snapshot = lot.model_copy(deep=True)

    replan()
    return snapshot


def mark_notifications_read(user: UserPublic, ids: Optional[List[str]] = None) -> int:
    with mutate() as state:
        inbox = state.notifications.get(user.username, [])
        wanted = set(ids) if ids else None
        touched = 0
        for item in inbox:
            if (wanted is None or item.id in wanted) and not item.read:
                item.read = True
                touched += 1
        return touched


# --------------------------------------------------------------------------- #
# Scoped reads with explicit 403s
# --------------------------------------------------------------------------- #
def state_envelope(user: UserPublic, since: Optional[int]) -> Dict[str, object]:
    """The payload every view polls, assembled for exactly one role."""
    with read() as state:
        current = _VERSION
        planning = is_planning()
        changed = since is None or int(since) != current

        data: Dict[str, object] = {}
        if changed:
            if user.role == Role.FARMER:
                data = farmer_view(state, user.username)
            elif user.role == Role.DRIVER and user.ref_id:
                data = driver_view(state, user.ref_id)
            elif user.role == Role.BUYER and user.ref_id:
                data = buyer_view(state, user.ref_id)
            elif user.role == Role.COORDINATOR:
                data = coordinator_view(state)

        inbox = state.notifications.get(user.username, [])
        return {
            "version": current,
            "changed": changed,
            "planning": planning,
            "notifications": [n.model_dump() for n in inbox[:20]],
            "unread": sum(1 for n in inbox if not n.read),
            "role": user.role.value,
            "user": user.model_dump(),
            "server_time": _now_iso(),
            "data": data,
        }


def trip_detail(user: UserPublic, truck_id: str) -> Dict[str, object]:
    """One trip, if this account has any business seeing it."""
    with read() as state:
        trip = _trip_or_404(state, truck_id)
        allowed = False
        if user.role == Role.COORDINATOR:
            allowed = True
        elif user.role == Role.DRIVER and user.ref_id == truck_id:
            allowed = True
        elif user.role == Role.BUYER and trip.route is not None:
            allowed = trip.route.buyer_id == user.ref_id
        elif user.role == Role.FARMER and trip.route is not None:
            mine = {f.id for f in state.dataset.farmers if f.owner == user.username}
            allowed = bool(mine & set(trip.route.farmer_ids))
        if not allowed:
            raise WorkflowError(403, f"account '{user.username}' may not view trip '{truck_id}'")

        if user.role in (Role.DRIVER, Role.COORDINATOR):
            return driver_view(state, truck_id)

        route = trip.route
        return {
            "trip": {
                "truck_id": trip.truck_id,
                "status": trip.status.value,
                "buyer_id": route.buyer_id if route else None,
                "buyer_name": route.buyer_name if route else None,
                "pickups": len(route.farmer_ids) if route else 0,
                "delivery_clock": minutes_to_clock(route.delivery_min) if route else None,
            }
        }


def farmer_detail(user: UserPublic, farmer_id: str) -> Dict[str, object]:
    """One lot with its payout. Deny by default: self or coordinator only.

    Drivers and buyers get the slice they need through their own view, which
    carries contact details but never a payout. Opening this endpoint to them
    as well would be the easy way to leak one farmer's price to another.
    """
    with read() as state:
        farmer = state.dataset.farmer_by_id(farmer_id)
        if farmer is None:
            raise WorkflowError(404, f"no lot '{farmer_id}'")
        is_self = user.role == Role.FARMER and farmer.owner == user.username
        if not (is_self or user.role == Role.COORDINATOR):
            raise WorkflowError(
                403, f"account '{user.username}' may not read lot '{farmer_id}'"
            )
        return {"lot": _lot_card(state, farmer, _payout_index(state))}
