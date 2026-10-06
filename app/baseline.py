"""Naive greedy planner: the control group for the optimiser.

What it does, in order, one truck at a time:

1. Start at the depot and repeatedly drive to the **nearest unvisited farmer
   whose lot still fits**, until nothing fits any more.
2. Then drive to the nearest buyer that wants whichever crop makes up the
   biggest share of the load.
3. Then drive home.

What it deliberately ignores -- and this is the whole point of having it:

* **Spoilage deadlines.** It will happily arrive at a leafy-greens lot six
  hours after it was cut. The lot is still loaded and still counted as moved,
  so the loss shows up in the metrics instead of being hidden by a constraint.
* **Buyer delivery windows.** It drives to the buyer whenever it finishes. If
  it gets there early it waits (a truck cannot deliver through a shut gate);
  if it gets there late, the delivery is simply late.
* **Crop/buyer matching across a mixed load.** One buyer is chosen per truck,
  so anything on board that the buyer does not want is flagged as rejected and
  ends up back at the middleman price.

It is a greedy planner, not a straw man: "nearest lot that *fits*" rather than
"nearest lot, then stop" keeps the trucks genuinely full, so the comparison
with the optimiser is about *route quality and deadline awareness* rather than
about one planner simply moving less produce.

Returns the same :class:`~app.models.PlanResult` shape as
:func:`app.solver.solve`, which is what makes the two directly comparable.
"""

from __future__ import annotations

import time as _time
from collections import Counter
from typing import Dict, List, Optional

from .geo import Point, road_distance_m, travel_minutes
from .models import (
    Buyer,
    Dataset,
    Farmer,
    PlanResult,
    RouteResult,
    Stop,
    StopKind,
    Truck,
    UnservedFarmer,
    UnservedReason,
    minutes_to_clock,
)
from .solver import SERVICE_TIME_MIN

MODE = "baseline"


def _point(obj) -> Point:
    return Point(obj.lat, obj.lng, getattr(obj, "id", ""))


def _unserved(
    farmer: Farmer, reason: str, code: UnservedReason = UnservedReason.OTHER,
    best_offer: Optional[float] = None,
) -> UnservedFarmer:
    return UnservedFarmer(
        farmer_id=farmer.id,
        name=farmer.name,
        village=farmer.village,
        crop=farmer.crop,
        quantity_kg=farmer.quantity_kg,
        reason=reason,
        reason_code=code,
        best_offer_per_kg=best_offer if code == UnservedReason.PRICE else None,
        min_price_per_kg=farmer.min_price_per_kg if code == UnservedReason.PRICE else None,
    )


def _buyer_takes(buyer: Buyer, farmer: Farmer) -> bool:
    """The one matching rule, applied here exactly as the solver applies it."""
    return buyer.wants(farmer.crop) and buyer.price_for(farmer.crop) >= farmer.min_price_per_kg


def _pick_buyer(
    dataset: Dataset,
    position: Point,
    load_by_crop: Counter,
    load_kg: int,
    remaining_demand: Dict[str, int],
    picked: List[Farmer],
) -> Optional[Buyer]:
    """Nearest buyer wanting the truck's majority crop, demand permitting.

    Preference order, all of it myopic by design:
    nearest buyer that wants the majority crop *and* still has room for the
    whole load -> nearest buyer that wants the majority crop -> nearest buyer
    with any interest in anything on board -> nearest buyer at all.
    """
    if not dataset.buyers:
        return None

    majority_crop = max(load_by_crop.items(), key=lambda kv: (kv[1], kv[0]))[0]
    # "Wants the crop" now means "wants it and will pay for the lots on board",
    # scored by how many kilos this buyer could actually take.
    wants_majority = [
        b for b in dataset.buyers
        if b.wants(majority_crop)
        and any(_buyer_takes(b, f) for f in picked if f.crop == majority_crop)
    ]
    if not wants_majority:
        wants_majority = [b for b in dataset.buyers if b.wants(majority_crop)]
    with_room = [b for b in wants_majority if remaining_demand.get(b.id, 0) >= load_kg]
    wants_anything = [
        b for b in dataset.buyers if any(_buyer_takes(b, f) for f in picked)
    ]

    for candidates in (with_room, wants_majority, wants_anything, dataset.buyers):
        if candidates:
            return min(
                candidates, key=lambda b: (road_distance_m(position, _point(b)), b.id)
            )
    return None


def plan(dataset: Dataset, **_ignored) -> PlanResult:
    """Run the greedy planner. ``**_ignored`` keeps the solver's call signature.

    Accepting and discarding keyword arguments such as ``time_limit_seconds``
    means callers can swap the two planners without special-casing either.
    """
    started = _time.perf_counter()

    if not dataset.farmers:
        return PlanResult(
            mode=MODE,
            solver_status="GREEDY_NO_FARMERS",
            solve_seconds=round(_time.perf_counter() - started, 3),
            notes=["No farmers have produce listed."],
        )
    if not dataset.trucks:
        return PlanResult(
            mode=MODE,
            unserved=[_unserved(f, "no truck in the fleet") for f in dataset.farmers],
            solver_status="GREEDY_NO_TRUCKS",
            solve_seconds=round(_time.perf_counter() - started, 3),
            notes=["No trucks available: every lot stays on the farm."],
        )
    if not dataset.buyers:
        return PlanResult(
            mode=MODE,
            unserved=[_unserved(f, "no buyer is accepting deliveries") for f in dataset.farmers],
            solver_status="GREEDY_NO_BUYERS",
            solve_seconds=round(_time.perf_counter() - started, 3),
            notes=["No buyer is accepting deliveries: nothing can be routed."],
        )

    # Even a naive planner does not collect produce nobody will buy at the
    # farmer's price, so the price rule is applied here too -- otherwise the
    # comparison would credit the baseline with kilos it could never sell.
    remaining: List[Farmer] = []
    unsellable: List[UnservedFarmer] = []
    for farmer in dataset.active_farmers():
        if dataset.buyers_for_lot(farmer):
            remaining.append(farmer)
        elif dataset.buyers_for_crop(farmer.crop):
            unsellable.append(
                _unserved(
                    farmer,
                    f"no buyer will pay {farmer.min_price_per_kg:.2f}/kg for "
                    f"{farmer.crop.replace('_', ' ')} today",
                    UnservedReason.PRICE,
                    dataset.best_offer_for_crop(farmer.crop),
                )
            )
        else:
            unsellable.append(
                _unserved(
                    farmer,
                    f"no buyer is accepting {farmer.crop} today",
                    UnservedReason.NO_BUYER,
                )
            )

    remaining_demand: Dict[str, int] = {b.id: b.demand_kg for b in dataset.buyers}
    routes: List[RouteResult] = []
    notes: List[str] = []

    for truck in dataset.trucks:
        route = _greedy_route(dataset, truck, remaining, remaining_demand)
        if route is not None:
            routes.append(route)

    unserved = unsellable + [
        _unserved(
            f,
            "greedy fleet ran out of capacity before reaching this lot "
            f"({f.quantity_kg} kg did not fit in any remaining truck)",
            UnservedReason.CAPACITY,
        )
        for f in remaining
    ]

    total_distance_m = sum(r.distance_m for r in routes)
    late_deliveries = sum(1 for r in routes if not r.delivery_on_time)
    spoiled_stops = sum(r.deadline_misses for r in routes)
    if spoiled_stops:
        notes.append(
            f"{spoiled_stops} pickup(s) happened after the crop's spoilage deadline: "
            "the greedy planner never checks it."
        )
    if late_deliveries:
        notes.append(f"{late_deliveries} delivery(ies) missed the buyer's time window.")
    rejected = sum(r.kg_rejected for r in routes)
    if rejected:
        notes.append(
            f"{rejected} kg arrived at a buyer that does not want that crop "
            "(one buyer per truck, chosen after loading)."
        )

    return PlanResult(
        mode=MODE,
        routes=routes,
        unserved=unserved,
        total_distance_m=total_distance_m,
        total_distance_km=round(total_distance_m / 1000.0, 2),
        total_duration_min=sum(r.duration_min for r in routes),
        total_cost=round(sum(r.cost for r in routes), 2),
        farmers_served=sum(len(r.farmer_ids) for r in routes),
        kg_moved=sum(r.load_kg for r in routes),
        trucks_used=len(routes),
        solver_status="GREEDY_COMPLETE",
        solve_seconds=round(_time.perf_counter() - started, 3),
        notes=notes,
    )


def _greedy_route(
    dataset: Dataset,
    truck: Truck,
    remaining: List[Farmer],
    remaining_demand: Dict[str, int],
) -> Optional[RouteResult]:
    """Fill one truck nearest-first, then deliver. Mutates ``remaining``."""
    depot = Point(truck.depot_lat, truck.depot_lng, "depot")
    position = depot
    clock = 0
    load_kg = 0
    cum_distance_m = 0
    stops: List[Stop] = [
        Stop(
            seq=0,
            kind=StopKind.DEPOT,
            ref_id="DEPOT",
            name="Depot",
            lat=depot.lat,
            lng=depot.lng,
            arrival_min=0,
            arrival_clock=minutes_to_clock(0),
            departure_min=0,
            load_kg_after=0,
        )
    ]
    picked: List[Farmer] = []
    load_by_crop: Counter = Counter()
    seq = 1

    while True:
        space = truck.capacity_kg - load_kg
        candidates = [f for f in remaining if f.quantity_kg <= space]
        if not candidates:
            break
        # Nearest lot that fits; farmer id breaks ties so runs are reproducible.
        nearest = min(candidates, key=lambda f: (road_distance_m(position, _point(f)), f.id))

        leg_m = road_distance_m(position, _point(nearest))
        drive = travel_minutes(leg_m, truck.speed_kmph)
        # A truck cannot load produce that has not been cut and packed yet, so
        # it waits. It does *not* check whether the lot will have spoiled.
        arrival = max(clock + drive, nearest.ready_from)
        wait = max(0, arrival - (clock + drive))
        cum_distance_m += leg_m
        load_kg += nearest.quantity_kg
        deadline = dataset.spoilage_deadline(nearest)
        past = max(0, arrival - deadline)

        stops.append(
            Stop(
                seq=seq,
                kind=StopKind.FARMER,
                ref_id=nearest.id,
                name=nearest.name,
                lat=nearest.lat,
                lng=nearest.lng,
                arrival_min=arrival,
                arrival_clock=minutes_to_clock(arrival),
                departure_min=arrival + SERVICE_TIME_MIN,
                service_min=SERVICE_TIME_MIN,
                wait_min=wait,
                leg_distance_m=leg_m,
                cum_distance_m=cum_distance_m,
                load_kg_after=load_kg,
                village=nearest.village,
                crop=nearest.crop,
                quantity_kg=nearest.quantity_kg,
                ready_from=nearest.ready_from,
                spoilage_deadline_min=deadline,
                minutes_past_deadline=past,
                on_time=past == 0,
            )
        )

        clock = arrival + SERVICE_TIME_MIN
        position = _point(nearest)
        picked.append(nearest)
        load_by_crop[nearest.crop] += nearest.quantity_kg
        remaining.remove(nearest)
        seq += 1

    if not picked:
        return None  # truck never left the yard

    buyer = _pick_buyer(dataset, position, load_by_crop, load_kg, remaining_demand, picked)
    assert buyer is not None  # dataset.buyers is non-empty by the time we get here

    leg_m = road_distance_m(position, _point(buyer))
    cum_distance_m += leg_m
    arrival_at_buyer = clock + travel_minutes(leg_m, truck.speed_kmph)
    # Arriving before the dock opens means waiting, not delivering early.
    delivery_min = max(arrival_at_buyer, buyer.window_start)
    kg_rejected = sum(f.quantity_kg for f in picked if not _buyer_takes(buyer, f))

    stops.append(
        Stop(
            seq=seq,
            kind=StopKind.BUYER,
            ref_id=buyer.id,
            name=buyer.name,
            lat=buyer.lat,
            lng=buyer.lng,
            arrival_min=delivery_min,
            arrival_clock=minutes_to_clock(delivery_min),
            departure_min=delivery_min,
            wait_min=max(0, delivery_min - arrival_at_buyer),
            leg_distance_m=leg_m,
            cum_distance_m=cum_distance_m,
            load_kg_after=load_kg,
        )
    )

    # Flag the lots this buyer will not take, now that the buyer is known --
    # either the wrong crop, or below that farmer's minimum price.
    takes = {f.id: _buyer_takes(buyer, f) for f in picked}
    for stop in stops:
        if stop.kind == StopKind.FARMER and stop.crop is not None:
            stop.crop_accepted = takes.get(stop.ref_id, False)

    remaining_demand[buyer.id] = max(0, remaining_demand.get(buyer.id, 0) - load_kg)

    return_leg_m = road_distance_m(_point(buyer), depot)
    cum_distance_m += return_leg_m
    home_arrival = delivery_min + travel_minutes(return_leg_m, truck.speed_kmph)
    stops.append(
        Stop(
            seq=seq + 1,
            kind=StopKind.DEPOT,
            ref_id="DEPOT",
            name="Depot (return)",
            lat=depot.lat,
            lng=depot.lng,
            arrival_min=home_arrival,
            arrival_clock=minutes_to_clock(home_arrival),
            departure_min=home_arrival,
            leg_distance_m=return_leg_m,
            cum_distance_m=cum_distance_m,
            load_kg_after=0,
        )
    )

    return RouteResult(
        truck_id=truck.id,
        truck_capacity_kg=truck.capacity_kg,
        buyer_id=buyer.id,
        buyer_name=buyer.name,
        stops=stops,
        farmer_ids=[f.id for f in picked],
        load_kg=load_kg,
        kg_rejected=kg_rejected,
        utilisation_pct=round(100.0 * load_kg / truck.capacity_kg, 1),
        distance_m=cum_distance_m,
        distance_km=round(cum_distance_m / 1000.0, 2),
        start_min=0,
        delivery_min=delivery_min,
        end_min=home_arrival,
        duration_min=home_arrival,
        cost=round(cum_distance_m / 1000.0 * truck.cost_per_km, 2),
        delivery_on_time=buyer.window_start <= delivery_min <= buyer.window_end,
        deadline_misses=sum(
            1 for s in stops if s.kind == StopKind.FARMER and s.minutes_past_deadline > 0
        ),
    )
