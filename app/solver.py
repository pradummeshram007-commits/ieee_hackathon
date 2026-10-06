"""OR-Tools vehicle routing model: the optimiser at the heart of KisanRoute.

FORMULATION (and why this one)
==============================
Node layout, in this order, so that **node 0 is the depot**::

    [ depots ] [ one node per modelled farmer ] [ one END node per vehicle ]

A *vehicle* is a ``(truck, buyer)`` pair.  Truck T1 with three buyers in the
dataset becomes three OR-Tools vehicles, each of which starts at T1's depot and
**ends at its buyer's location**, plus a constraint that at most one of those
three clones may be used.  That keeps "which buyer does this truck serve?" a
decision the solver makes, not one we hard-code.

Modelling the buyer as the route's *end node* rather than as a mandatory final
stop is the cleaner of the two options the brief allows, for four reasons:

1. "The buyer is visited last, exactly once" becomes structural.  The
   alternative needs the buyer as a regular node plus precedence constraints
   plus a forced-visit disjunction, all of which can interact badly with the
   farmer drop penalties and quietly make the model infeasible.
2. The load dimension stays monotonically increasing: farmer nodes have
   positive demand, nothing has negative demand, and truck capacity is just
   the maximum cumulative pickup.  No pickup-and-delivery pairing needed.
3. The buyer's delivery window is literally the cumulative-time variable at
   the route end, so it is one ``SetRange`` call on ``CumulVar(End(v))``.
4. Per-vehicle end nodes make each clone's geography explicit, so reading the
   solution back needs no guessing about which buyer a truck actually served.

The one wrinkle of ending at the buyer is that a real truck still has to get
home.  We fold the buyer -> depot leg into the **cost** of the final arc, so
the objective prices the true round trip, while the **time** dimension keeps
measuring arrival at the buyer (which is what the delivery window constrains).

UNITS
=====
Distance is metres, time is minutes, and the objective is in *paise*
(1/100 INR) so that per-arc rounding never swallows a meaningful cost
difference.  Everything crossing into OR-Tools is an ``int``.

ROBUSTNESS
==========
Every farmer node sits in its own ``AddDisjunction`` with a large penalty, so
an impossible farmer is **dropped** and the rest of the plan still solves.  The
demo cannot be made to return "no solution" by bad data: farmers that are
infeasible by construction are filtered out before the model is built and
reported with a reason, and if the search somehow fails we still return a
well-formed (empty) plan with the solver status attached.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set, Tuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .geo import Point, build_distance_matrix, build_time_matrix, travel_minutes
from .models import (
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

MODE = "optimized"

#: Minutes a truck spends loading at one farmer's gate.
SERVICE_TIME_MIN = 15

#: Default wall-clock budget for the search.
DEFAULT_TIME_LIMIT_SECONDS = 8.0

#: Planning horizon in minutes from plan start (24 h). Upper bound of the time
#: dimension; every window must fit inside it.
HORIZON_MIN = 1440

#: Most a truck may idle at a single node (waiting for a harvest to be ready,
#: or for a buyer's dock to open). Generous enough never to be the binding
#: constraint, bounded so propagation stays cheap.
MAX_SLACK_MIN = 480

#: Objective unit: paise per cost unit (INR * 100).
COST_SCALE = 100

#: Penalty for dropping a farmer, in INR: a flat insult plus a per-kg term.
#: Sized to dwarf any routing detour (a whole tour costs a few thousand INR),
#: so the optimiser drops a lot only when it is genuinely infeasible or the
#: fleet has run out of capacity -- and when it must choose, the per-kg term
#: makes it give up the smallest lots first.
DROP_PENALTY_BASE_INR = 5_000
DROP_PENALTY_PER_KG_INR = 60

RANDOM_SEED = 42

_STATUS_BY_CODE: Dict[int, str] = {
    code: name for name, code in routing_enums_pb2.RoutingSearchStatus.Value.items()
}


def _status_name(code: int) -> str:
    return _STATUS_BY_CODE.get(code, f"UNKNOWN_STATUS_{code}")


@dataclass(frozen=True)
class _Vehicle:
    """One OR-Tools vehicle: a physical truck committed to one buyer."""

    index: int
    truck: Truck
    buyer_index: int
    home_node: int
    end_node: int


def _empty_plan(
    status: str, note: str, unserved: List[UnservedFarmer], elapsed: float
) -> PlanResult:
    return PlanResult(
        mode=MODE,
        unserved=unserved,
        solver_status=status,
        solve_seconds=round(elapsed, 3),
        notes=[note],
    )


def _unserved(
    dataset: Dataset,
    farmer: Farmer,
    reason: str,
    code: UnservedReason = UnservedReason.OTHER,
) -> UnservedFarmer:
    return UnservedFarmer(
        farmer_id=farmer.id,
        name=farmer.name,
        village=farmer.village,
        crop=farmer.crop,
        quantity_kg=farmer.quantity_kg,
        reason=reason,
        reason_code=code,
        best_offer_per_kg=(
            dataset.best_offer_for_crop(farmer.crop) if code == UnservedReason.PRICE else None
        ),
        min_price_per_kg=(
            farmer.min_price_per_kg if code == UnservedReason.PRICE else None
        ),
    )


def price_block_message(dataset: Dataset, farmer: Farmer) -> str:
    """The sentence a farmer sees when nobody will pay their minimum."""
    crop = farmer.crop.replace("_", " ")
    best = dataset.best_offer_for_crop(farmer.crop)
    offer = f"\u20b9{best:.2f}/kg" if best is not None else "no offer at all"
    return (
        f"No buyer at your price. Best offer for {crop} today: {offer}. "
        f"Your {crop} spoils at {spoils_phrase(dataset.spoilage_deadline(farmer))}."
    )


def spoils_phrase(deadline_min: int) -> str:
    """'11:35' today, or '08:50, 3 days from now' for long-life crops.

    The dashboard's '08:50+3d' is fine for a coordinator but reads like a typo
    in a sentence addressed to a farmer.
    """
    clock = minutes_to_clock(deadline_min).split("+")[0]
    days = (6 * 60 + deadline_min) // 1440
    if days <= 0:
        return clock
    return f"{clock}, {days} day{'s' if days > 1 else ''} from now"


def _prefilter(dataset: Dataset) -> Tuple[List[Farmer], List[UnservedFarmer]]:
    """Separate farmers that can be modelled from ones that cannot.

    Two kinds of farmer must never reach the routing model:

    * **No outlet.** If no buyer wants the crop there is no vehicle whose end
      node can accept it. An empty allowed-vehicle set is silently read as
      "all vehicles allowed" by OR-Tools, which would route the lot to a buyer
      that does not want it.
    * **Empty pickup window.** ``min(ready_until, spoilage_deadline)`` below
      ``ready_from`` means the lot spoils before the farmer can hand it over.
      Feeding an inverted range to ``CumulVar.SetRange`` makes the *whole*
      model infeasible, dropped node or not.
    """
    modelled: List[Farmer] = []
    rejected: List[UnservedFarmer] = []

    for farmer in dataset.active_farmers():
        if farmer.crop not in dataset.crops:
            rejected.append(
                _unserved(dataset, farmer, f"crop '{farmer.crop}' is not in the catalogue")
            )
            continue
        if not dataset.buyers_for_crop(farmer.crop):
            rejected.append(
                _unserved(
                    dataset, farmer,
                    f"no buyer is accepting {farmer.crop} today",
                    UnservedReason.NO_BUYER,
                )
            )
            continue
        if not dataset.buyers_for_lot(farmer):
            # Buyers want the crop, but none of them will meet this farmer's
            # floor. That is a price problem, not a routing one, and it gets
            # its own reason so the farmer is offered an Edit price button
            # rather than an apology about truck capacity.
            rejected.append(
                _unserved(
                    dataset, farmer,
                    price_block_message(dataset, farmer),
                    UnservedReason.PRICE,
                )
            )
            continue
        lower, upper = dataset.pickup_window(farmer)
        if upper < lower:
            deadline = dataset.spoilage_deadline(farmer)
            rejected.append(
                _unserved(
                    dataset,
                    farmer,
                    f"lot spoils at minute {deadline} ({minutes_to_clock(deadline)}) but is "
                    f"not ready for pickup until minute {lower} ({minutes_to_clock(lower)})",
                    UnservedReason.WINDOW,
                )
            )
            continue
        if lower > HORIZON_MIN:
            rejected.append(
                _unserved(
                    dataset, farmer,
                    f"pickup window opens past the {HORIZON_MIN} min horizon",
                    UnservedReason.WINDOW,
                )
            )
            continue
        modelled.append(farmer)

    return modelled, rejected


def _make_cost_callback(manager, distance_m: Sequence[Sequence[int]], vehicle: _Vehicle):
    """Arc cost in paise for one vehicle, including its drive home.

    The buyer -> depot leg is added to every arc that *enters* this vehicle's
    end node, which is the only arc that can be the route's last one.  OR-Tools
    charges nothing for an empty route's start -> end arc, so an unused truck
    stays free.
    """
    rate = vehicle.truck.cost_per_km
    end_node = vehicle.end_node
    return_leg_m = distance_m[end_node][vehicle.home_node]

    def callback(from_index: int, to_index: int) -> int:
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        metres = distance_m[i][j]
        if j == end_node:
            metres += return_leg_m
        return int(round(metres * rate * COST_SCALE / 1000.0))

    return callback


def _make_time_callback(manager, time_matrix: Sequence[Sequence[int]]):
    """Minutes to serve node ``i`` and then drive to node ``j``."""

    def callback(from_index: int, to_index: int) -> int:
        return time_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    return callback


def solve(
    dataset: Dataset, time_limit_seconds: float = DEFAULT_TIME_LIMIT_SECONDS
) -> PlanResult:
    """Build and solve the pickup-routing model. Always returns a plan."""
    started = _time.perf_counter()

    if not dataset.active_farmers():
        return _empty_plan("NO_FARMERS", "No farmers have produce listed.", [], _time.perf_counter() - started)
    if not dataset.trucks:
        return _empty_plan(
            "NO_TRUCKS",
            "No trucks available: every lot stays on the farm.",
            [
                _unserved(dataset, f, "no truck in the fleet", UnservedReason.NO_TRUCK)
                for f in dataset.active_farmers()
            ],
            _time.perf_counter() - started,
        )
    if not dataset.buyers:
        return _empty_plan(
            "NO_BUYERS",
            "No buyer is accepting deliveries: nothing can be routed.",
            [
                _unserved(dataset, f, "no buyer is accepting deliveries", UnservedReason.NO_BUYER)
                for f in dataset.active_farmers()
            ],
            _time.perf_counter() - started,
        )

    notes: List[str] = []
    modelled, unserved = _prefilter(dataset)
    if not modelled:
        return _empty_plan(
            "NO_FEASIBLE_FARMERS",
            "Every listed lot is infeasible before routing even starts.",
            unserved,
            _time.perf_counter() - started,
        )

    # ------------------------------------------------------------------ #
    # Nodes: depots first (so node 0 is a depot), then farmers, then one
    # end node per vehicle.
    # ------------------------------------------------------------------ #
    points: List[Point] = []
    depot_node_by_coord: Dict[Tuple[float, float], int] = {}
    for truck in dataset.trucks:
        key = (round(truck.depot_lat, 6), round(truck.depot_lng, 6))
        if key not in depot_node_by_coord:
            depot_node_by_coord[key] = len(points)
            points.append(Point(truck.depot_lat, truck.depot_lng, "depot"))

    farmer_node: List[int] = []
    farmer_by_node: Dict[int, Farmer] = {}
    for farmer in modelled:
        node = len(points)
        farmer_node.append(node)
        farmer_by_node[node] = farmer
        points.append(Point(farmer.lat, farmer.lng, farmer.id))

    vehicles: List[_Vehicle] = []
    for truck in dataset.trucks:
        home = depot_node_by_coord[(round(truck.depot_lat, 6), round(truck.depot_lng, 6))]
        for buyer_index, buyer in enumerate(dataset.buyers):
            end_node = len(points)
            points.append(Point(buyer.lat, buyer.lng, f"{buyer.id}:{truck.id}"))
            vehicles.append(
                _Vehicle(
                    index=len(vehicles),
                    truck=truck,
                    buyer_index=buyer_index,
                    home_node=home,
                    end_node=end_node,
                )
            )

    node_count = len(points)
    distance_m = build_distance_matrix(points)

    demand_kg = [0] * node_count
    service_min = [0] * node_count
    for node, farmer in farmer_by_node.items():
        demand_kg[node] = farmer.quantity_kg
        service_min[node] = SERVICE_TIME_MIN

    # ------------------------------------------------------------------ #
    # Routing model
    # ------------------------------------------------------------------ #
    manager = pywrapcp.RoutingIndexManager(
        node_count,
        len(vehicles),
        [v.home_node for v in vehicles],
        [v.end_node for v in vehicles],
    )
    routing = pywrapcp.RoutingModel(manager)
    solver = routing.solver()
    # Hold references so the SWIG directors behind the callbacks outlive the
    # model-building scope.
    callbacks: List[object] = []

    # Cost: per-vehicle, because each truck has its own INR/km and its own
    # drive home from its own buyer.
    for vehicle in vehicles:
        callback = _make_cost_callback(manager, distance_m, vehicle)
        callbacks.append(callback)
        routing.SetArcCostEvaluatorOfVehicle(
            routing.RegisterTransitCallback(callback), vehicle.index
        )

    # Capacity dimension on quantity_kg. Cumulative load at a node is the load
    # *before* picking up there; it is fixed to 0 at the depot.
    def demand_callback(index: int) -> int:
        return demand_kg[manager.IndexToNode(index)]

    callbacks.append(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(demand_callback),
        0,  # no slack: a truck cannot un-load mid-route
        [v.truck.capacity_kg for v in vehicles],
        True,  # start cumul (load leaving the depot) is zero
        "Load",
    )
    load_dim = routing.GetDimensionOrDie("Load")

    # Time dimension, with the 15 min farmer service time folded into every
    # outgoing arc so that CumulVar == arrival time. Slack lets a truck wait
    # for a harvest to be ready or a buyer's dock to open.
    time_callback_by_speed: Dict[float, int] = {}
    for vehicle in vehicles:
        speed = vehicle.truck.speed_kmph
        if speed not in time_callback_by_speed:
            matrix = build_time_matrix(distance_m, speed, service_min)
            callback = _make_time_callback(manager, matrix)
            callbacks.append(callback)
            time_callback_by_speed[speed] = routing.RegisterTransitCallback(callback)

    routing.AddDimensionWithVehicleTransits(
        [time_callback_by_speed[v.truck.speed_kmph] for v in vehicles],
        MAX_SLACK_MIN,
        HORIZON_MIN,
        False,  # a truck may leave the depot later than minute 0
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # Farmer pickup windows. The upper bound is the spoilage deadline whenever
    # that bites before the farmer's own cut-off -- this is the constraint the
    # whole project exists to respect.
    for position, farmer in enumerate(modelled):
        index = manager.NodeToIndex(farmer_node[position])
        lower, upper = dataset.pickup_window(farmer)
        time_dim.CumulVar(index).SetRange(int(lower), int(min(upper, HORIZON_MIN)))

    # Buyer delivery windows, applied to each route's end time.
    for vehicle in vehicles:
        buyer = dataset.buyers[vehicle.buyer_index]
        end_index = routing.End(vehicle.index)
        time_dim.CumulVar(end_index).SetRange(
            int(buyer.window_start), int(min(buyer.window_end, HORIZON_MIN))
        )
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.Start(vehicle.index)))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(end_index))

    # Matching rule: a lot may only ride a truck whose buyer wants that crop
    # AND offers at least the farmer's minimum price. Both halves go through
    # the same VehicleVar domain restriction, so an unaffordable match is not
    # merely discouraged by the objective -- it is not in the search space.
    # (RoutingModel.SetAllowedVehiclesForIndex is not callable from Python in
    # OR-Tools 9.x -- its absl::Span typemap is unwired -- so we restrict the
    # VehicleVar domain directly. -1 must stay in the domain or the node could
    # not be dropped.)
    for position, farmer in enumerate(modelled):
        index = manager.NodeToIndex(farmer_node[position])
        allowed = [
            vehicle.index
            for vehicle in vehicles
            if dataset.buyers[vehicle.buyer_index].wants(farmer.crop)
            and dataset.buyers[vehicle.buyer_index].price_for(farmer.crop)
            >= farmer.min_price_per_kg
        ]
        routing.VehicleVar(index).SetValues([-1] + allowed)

        # Drop penalty: this is what guarantees a solution exists.
        penalty = int(
            (DROP_PENALTY_BASE_INR + DROP_PENALTY_PER_KG_INR * farmer.quantity_kg) * COST_SCALE
        )
        routing.AddDisjunction([index], penalty)

    # Each physical truck may take at most one of its per-buyer clones. A clone
    # is "used" exactly when the node after its start is not its end.
    vehicles_by_truck: Dict[str, List[int]] = {}
    for vehicle in vehicles:
        vehicles_by_truck.setdefault(vehicle.truck.id, []).append(vehicle.index)
    for clone_indices in vehicles_by_truck.values():
        if len(clone_indices) > 1:
            in_use = [
                solver.IsDifferentCstVar(routing.NextVar(routing.Start(v)), routing.End(v))
                for v in clone_indices
            ]
            solver.Add(solver.Sum(in_use) <= 1)

    # A buyer cannot be sent more than it asked for, summed over every truck
    # that delivers to it. The load at a route's end node is what it delivers.
    vehicles_by_buyer: Dict[int, List[int]] = {}
    for vehicle in vehicles:
        vehicles_by_buyer.setdefault(vehicle.buyer_index, []).append(vehicle.index)
    for buyer_index, clone_indices in vehicles_by_buyer.items():
        delivered = [load_dim.CumulVar(routing.End(v)) for v in clone_indices]
        solver.Add(solver.Sum(delivered) <= int(dataset.buyers[buyer_index].demand_kg))

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromMilliseconds(int(max(0.1, time_limit_seconds) * 1000))
    params.log_search = False

    reseed = getattr(solver, "ReSeed", None)
    if callable(reseed):  # pin the CP solver's RNG where the binding exposes it
        reseed(RANDOM_SEED)

    solution = routing.SolveWithParameters(params)
    status = _status_name(routing.status())
    elapsed = _time.perf_counter() - started

    if solution is None:
        notes.append(
            "Search returned no assignment; every lot is reported unserved. "
            "This should be unreachable because all farmer nodes are droppable."
        )
        unserved.extend(
            _unserved(dataset, f, f"solver returned no assignment ({status})") for f in modelled
        )
        return PlanResult(
            mode=MODE,
            unserved=unserved,
            solver_status=status,
            solve_seconds=round(elapsed, 3),
            notes=notes,
        )

    routes, served_nodes = _extract_routes(
        dataset=dataset,
        manager=manager,
        routing=routing,
        solution=solution,
        vehicles=vehicles,
        farmer_by_node=farmer_by_node,
        demand_kg=demand_kg,
        distance_m=distance_m,
        time_dim=time_dim,
        load_dim=load_dim,
    )

    shortfall = dataset.total_supply_kg() - dataset.total_capacity_kg()
    earliest_arrival = _earliest_arrival_minutes(dataset, modelled, farmer_node, distance_m, depot_node_by_coord)
    for position, farmer in enumerate(modelled):
        if farmer_node[position] not in served_nodes:
            text, code = _drop_reason(dataset, farmer, earliest_arrival[farmer.id], shortfall)
            unserved.append(_unserved(dataset, farmer, text, code))

    total_distance_m = sum(r.distance_m for r in routes)
    plan = PlanResult(
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
        solver_status=status,
        solve_seconds=round(elapsed, 3),
        notes=notes,
    )

    if shortfall > 0:
        plan.notes.append(
            f"Supply exceeds fleet capacity by {shortfall} kg "
            f"({dataset.total_supply_kg()} kg listed vs {dataset.total_capacity_kg()} kg of trucks), "
            "so some lots must be left behind by construction."
        )
    plan.notes.append(
        f"Objective {solution.ObjectiveValue() / COST_SCALE:,.2f} INR "
        "(routing cost plus drop penalties)."
    )
    return plan


def _earliest_arrival_minutes(
    dataset: Dataset,
    modelled: Sequence[Farmer],
    farmer_node: Sequence[int],
    distance_m: Sequence[Sequence[int]],
    depot_node_by_coord: Dict[Tuple[float, float], int],
) -> Dict[str, int]:
    """Fastest any truck in the fleet could reach each farmer from its depot."""
    depots = [
        (depot_node_by_coord[(round(t.depot_lat, 6), round(t.depot_lng, 6))], t.speed_kmph)
        for t in dataset.trucks
    ]
    earliest: Dict[str, int] = {}
    for position, farmer in enumerate(modelled):
        node = farmer_node[position]
        earliest[farmer.id] = min(
            travel_minutes(distance_m[home][node], speed) for home, speed in depots
        )
    return earliest


def _drop_reason(
    dataset: Dataset, farmer: Farmer, earliest_arrival: int, shortfall_kg: int
) -> Tuple[str, UnservedReason]:
    """Plain-language explanation, plus a code the UI can branch on."""
    lower, upper = dataset.pickup_window(farmer)
    deadline = dataset.spoilage_deadline(farmer)

    if earliest_arrival > upper:
        return (
            f"unreachable in time: the closest depot is {earliest_arrival} min away but the "
            f"pickup window shuts at minute {upper} ({minutes_to_clock(upper)}); "
            f"{farmer.crop} spoils at minute {deadline}",
            UnservedReason.WINDOW,
        )
    if upper == deadline and upper < farmer.ready_until:
        window = (
            f"pickup window {minutes_to_clock(lower)}-{minutes_to_clock(upper)}, closed early by "
            f"the {dataset.crops[farmer.crop].spoilage_hours:g} h spoilage limit"
        )
    else:
        window = (
            f"pickup window {minutes_to_clock(lower)}-{minutes_to_clock(upper)}, "
            "set by the farmer's own cut-off"
        )
    if shortfall_kg > 0:
        return (
            f"left behind: the fleet is {shortfall_kg} kg short of listed supply, so taking this "
            f"lot would mean giving up a larger one ({window})",
            UnservedReason.CAPACITY,
        )
    return (
        f"no feasible insertion: no truck could fit the detour within capacity ({window})",
        UnservedReason.CAPACITY,
    )


def _extract_routes(
    *,
    dataset: Dataset,
    manager,
    routing,
    solution,
    vehicles: Sequence[_Vehicle],
    farmer_by_node: Dict[int, Farmer],
    demand_kg: Sequence[int],
    distance_m: Sequence[Sequence[int]],
    time_dim,
    load_dim,
) -> Tuple[List[RouteResult], Set[int]]:
    """Turn the OR-Tools assignment into :class:`RouteResult` objects."""
    routes: List[RouteResult] = []
    served_nodes: Set[int] = set()

    for vehicle in vehicles:
        start_index = routing.Start(vehicle.index)
        if routing.IsEnd(solution.Value(routing.NextVar(start_index))):
            continue  # unused clone of this truck

        buyer = dataset.buyers[vehicle.buyer_index]
        speed = vehicle.truck.speed_kmph
        stops: List[Stop] = []
        farmer_ids: List[str] = []
        deadline_misses = 0
        cum_distance_m = 0

        index = start_index
        node = manager.IndexToNode(index)
        depart = int(solution.Min(time_dim.CumulVar(index)))
        stops.append(
            Stop(
                seq=0,
                kind=StopKind.DEPOT,
                ref_id=f"DEPOT-{vehicle.home_node}",
                name="Depot",
                lat=vehicle.truck.depot_lat,
                lng=vehicle.truck.depot_lng,
                arrival_min=depart,
                arrival_clock=minutes_to_clock(depart),
                departure_min=depart,
                load_kg_after=0,
            )
        )

        previous_node = node
        previous_departure = depart
        seq = 1
        index = solution.Value(routing.NextVar(index))

        while True:
            node = manager.IndexToNode(index)
            leg_m = distance_m[previous_node][node]
            cum_distance_m += leg_m
            drive = travel_minutes(leg_m, speed)
            arrival = int(solution.Min(time_dim.CumulVar(index)))
            wait = max(0, arrival - (previous_departure + drive))
            load_after = int(solution.Min(load_dim.CumulVar(index))) + int(demand_kg[node])

            if routing.IsEnd(index):
                stops.append(
                    Stop(
                        seq=seq,
                        kind=StopKind.BUYER,
                        ref_id=buyer.id,
                        name=buyer.name,
                        lat=buyer.lat,
                        lng=buyer.lng,
                        arrival_min=arrival,
                        arrival_clock=minutes_to_clock(arrival),
                        departure_min=arrival,
                        wait_min=wait,
                        leg_distance_m=leg_m,
                        cum_distance_m=cum_distance_m,
                        load_kg_after=load_after,
                    )
                )
                break

            farmer = farmer_by_node[node]
            deadline = dataset.spoilage_deadline(farmer)
            past = max(0, arrival - deadline)
            if past > 0:
                deadline_misses += 1
            farmer_ids.append(farmer.id)
            served_nodes.add(node)
            stops.append(
                Stop(
                    seq=seq,
                    kind=StopKind.FARMER,
                    ref_id=farmer.id,
                    name=farmer.name,
                    lat=farmer.lat,
                    lng=farmer.lng,
                    arrival_min=arrival,
                    arrival_clock=minutes_to_clock(arrival),
                    departure_min=arrival + SERVICE_TIME_MIN,
                    service_min=SERVICE_TIME_MIN,
                    wait_min=wait,
                    leg_distance_m=leg_m,
                    cum_distance_m=cum_distance_m,
                    load_kg_after=load_after,
                    village=farmer.village,
                    crop=farmer.crop,
                    quantity_kg=farmer.quantity_kg,
                    ready_from=farmer.ready_from,
                    spoilage_deadline_min=deadline,
                    minutes_past_deadline=past,
                    on_time=past == 0,
                    crop_accepted=buyer.wants(farmer.crop),
                )
            )
            previous_node = node
            previous_departure = arrival + SERVICE_TIME_MIN
            seq += 1
            index = solution.Value(routing.NextVar(index))

        # The drive home: priced inside the final arc's cost, reported here as
        # an explicit stop so the route reads depot -> farmers -> buyer -> depot.
        delivery_min = stops[-1].arrival_min
        return_leg_m = distance_m[vehicle.end_node][vehicle.home_node]
        cum_distance_m += return_leg_m
        home_arrival = delivery_min + travel_minutes(return_leg_m, speed)
        stops.append(
            Stop(
                seq=seq + 1,
                kind=StopKind.DEPOT,
                ref_id=f"DEPOT-{vehicle.home_node}",
                name="Depot (return)",
                lat=vehicle.truck.depot_lat,
                lng=vehicle.truck.depot_lng,
                arrival_min=home_arrival,
                arrival_clock=minutes_to_clock(home_arrival),
                departure_min=home_arrival,
                leg_distance_m=return_leg_m,
                cum_distance_m=cum_distance_m,
                load_kg_after=0,
            )
        )

        load_kg = stops[-2].load_kg_after
        routes.append(
            RouteResult(
                truck_id=vehicle.truck.id,
                truck_capacity_kg=vehicle.truck.capacity_kg,
                buyer_id=buyer.id,
                buyer_name=buyer.name,
                stops=stops,
                farmer_ids=farmer_ids,
                load_kg=load_kg,
                kg_rejected=0,  # crop compatibility is a hard constraint here
                utilisation_pct=round(100.0 * load_kg / vehicle.truck.capacity_kg, 1),
                distance_m=cum_distance_m,
                distance_km=round(cum_distance_m / 1000.0, 2),
                start_min=stops[0].arrival_min,
                delivery_min=delivery_min,
                end_min=home_arrival,
                duration_min=home_arrival - stops[0].arrival_min,
                cost=round(cum_distance_m / 1000.0 * vehicle.truck.cost_per_km, 2),
                delivery_on_time=buyer.window_start <= delivery_min <= buyer.window_end,
                deadline_misses=deadline_misses,
            )
        )

    routes.sort(key=lambda r: r.truck_id)
    return routes, served_nodes
