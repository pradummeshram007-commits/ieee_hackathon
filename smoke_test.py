#!/usr/bin/env python3
"""End-to-end smoke test for KisanRoute.

Runs the whole pipeline with no server and no network:

1. builds the seeded dataset and checks its invariants,
2. plans with the OR-Tools optimiser and with the greedy baseline,
3. prints a side-by-side table of every metric,
4. prints every truck's route with arrival times,
5. prints the farmer payout split,
6. replays a truck breakdown and diffs the re-plan,
7. verifies the optimiser's hard constraints and that it beats the baseline on
   total_km and kg_spoiled.

Usage::

    python smoke_test.py                       # 8 second solve, as specified
    KISANROUTE_TIME_LIMIT=2 python smoke_test.py   # quick run

Exits 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import os
import sys
from typing import List, Tuple

from app import baseline, metrics
from app.geo import Point, road_distance_m
from app.models import PlanResult, StopKind, minutes_to_clock
from app.seed import NAGPUR, build_dataset
from app.solver import solve

TIME_LIMIT = float(os.environ.get("KISANROUTE_TIME_LIMIT", "8"))
WIDTH = 104

#: Statuses that mean "we have a usable assignment".
GOOD_STATUSES = {
    "ROUTING_SUCCESS",
    "ROUTING_OPTIMAL",
    "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
}


class Checks:
    """Collects every assertion so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.rows: List[Tuple[bool, str, str, str]] = []

    def check(self, label: str, ok: bool, info: str = "", detail: str = "") -> bool:
        """Record one check.

        ``info`` is printed either way (the numbers behind the check); ``detail``
        only when it fails (usually the list of offending items).
        """
        self.rows.append((bool(ok), label, info, detail))
        return bool(ok)

    @property
    def failures(self) -> List[Tuple[bool, str, str, str]]:
        return [row for row in self.rows if not row[0]]

    def report(self) -> bool:
        heading("7. CHECKS")
        for ok, label, info, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            shown = info if ok else (detail or info)
            line = f"  [{mark}] {label}"
            print(line if not shown else f"{line}\n          {shown}")
        passed = len(self.rows) - len(self.failures)
        print(f"\n  {passed}/{len(self.rows)} checks passed")
        return not self.failures


def heading(title: str) -> None:
    print("\n" + "=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def fmt(value: float, unit: str) -> str:
    if unit in ("farmers", "kg", "trucks"):
        return f"{value:,.0f}"
    return f"{value:,.2f}"


# --------------------------------------------------------------------------- #
# Report sections
# --------------------------------------------------------------------------- #
def print_dataset(dataset) -> None:
    heading("1. DATASET")
    summary = dataset.summary()
    print(
        f"  {summary['farmers']} farmers in {len(summary['villages'])} villages "
        f"({', '.join(summary['villages'])})"
    )
    print(f"  {summary['buyers']} buyers, {summary['trucks']} trucks, plan starts {dataset.plan_start_label}")
    print(
        f"  supply {summary['total_supply_kg']:,} kg vs fleet capacity "
        f"{summary['total_capacity_kg']:,} kg = {summary['supply_vs_capacity_pct']}% "
        f"(buyer demand {summary['total_demand_kg']:,} kg)"
    )
    print()
    print(f"  {'crop':14} {'spoils':>8} {'base INR/kg':>12} {'lots':>5} {'kg':>8}")
    for name, crop in sorted(dataset.crops.items()):
        lots = [f for f in dataset.farmers if f.crop == name]
        print(
            f"  {name:14} {crop.spoilage_hours:>6.0f} h {crop.base_price_per_kg:>12.2f} "
            f"{len(lots):>5} {sum(f.quantity_kg for f in lots):>8,}"
        )
    print()
    print(f"  {'truck':6} {'capacity':>9} {'INR/km':>8} {'km/h':>6}")
    for truck in dataset.trucks:
        print(
            f"  {truck.id:6} {truck.capacity_kg:>7,} kg {truck.cost_per_km:>8.2f} "
            f"{truck.speed_kmph:>6.0f}"
        )
    print()
    for buyer in dataset.buyers:
        print(
            f"  {buyer.id} {buyer.name:28} {buyer.type.value:10} demand {buyer.demand_kg:>5,} kg  "
            f"window {minutes_to_clock(buyer.window_start)}-{minutes_to_clock(buyer.window_end)}  "
            f"wants {', '.join(buyer.crops_wanted)}"
        )


def print_table(report) -> None:
    heading("3. METRIC COMPARISON  (baseline greedy vs OR-Tools optimiser)")
    print(
        f"  {'metric':26} {'unit':>13} {'baseline':>13} {'optimised':>13} "
        f"{'delta':>12} {'improve':>9}  ok"
    )
    print("  " + "-" * (WIDTH - 4))
    for row in report.deltas:
        improvement = "n/a" if row.improvement_pct is None else f"{row.improvement_pct:+.1f}%"
        print(
            f"  {row.metric:26} {row.unit:>13} {fmt(row.baseline, row.unit):>13} "
            f"{fmt(row.optimized, row.unit):>13} {fmt(row.delta, row.unit):>12} "
            f"{improvement:>9}  {'Y' if row.better else 'n'}"
        )
    print("\n  Improvement is signed so that positive always favours the optimiser.")
    print("\n  Headline:")
    for line in report.headline:
        print(f"    - {line}")


def print_routes(dataset, plan: PlanResult, title: str) -> None:
    heading(title)
    print(
        f"  status {plan.solver_status} | {plan.solve_seconds:.3f}s | "
        f"{plan.trucks_used} trucks | {plan.farmers_served} farmers | "
        f"{plan.kg_moved:,} kg | {plan.total_distance_km:,.2f} km | INR {plan.total_cost:,.2f}"
    )
    for route in plan.routes:
        print()
        flag = "" if route.delivery_on_time else "   <-- OUTSIDE BUYER WINDOW"
        buyer = dataset.buyer_by_id(route.buyer_id) if route.buyer_id else None
        window = (
            f"{minutes_to_clock(buyer.window_start)}-{minutes_to_clock(buyer.window_end)}"
            if buyer
            else "n/a"
        )
        print(
            f"  {route.truck_id} -> {route.buyer_id} {route.buyer_name}  "
            f"load {route.load_kg:,}/{route.truck_capacity_kg:,} kg ({route.utilisation_pct:.0f}%)  "
            f"{route.distance_km:,.1f} km  INR {route.cost:,.0f}  "
            f"delivers {minutes_to_clock(route.delivery_min)} (window {window}){flag}"
        )
        print(
            f"    {'#':>2} {'arrive':>7} {'wait':>5} {'stop':26} {'crop':13} "
            f"{'kg':>6} {'load':>6} {'deadline':>9}"
        )
        for stop in route.stops:
            late = (
                f"  LATE +{stop.minutes_past_deadline}m"
                if stop.minutes_past_deadline
                else ""
            )
            rejected = "" if stop.crop_accepted else "  CROP NOT WANTED"
            deadline = (
                minutes_to_clock(stop.spoilage_deadline_min)
                if stop.spoilage_deadline_min is not None
                else "-"
            )
            label = stop.name if stop.kind != StopKind.FARMER else f"{stop.ref_id} {stop.name}"
            print(
                f"    {stop.seq:>2} {stop.arrival_clock:>7} {stop.wait_min:>5} {label[:26]:26} "
                f"{(stop.crop or '-'):13} {stop.quantity_kg:>6,} {stop.load_kg_after:>6,} "
                f"{deadline:>9}{late}{rejected}"
            )
    if plan.unserved:
        print(f"\n  UNSERVED ({len(plan.unserved)} lots, "
              f"{sum(u.quantity_kg for u in plan.unserved):,} kg):")
        for u in plan.unserved:
            print(f"    {u.farmer_id} {u.name:22} {u.crop:13} {u.quantity_kg:>5,} kg  {u.reason}")
    for note in plan.notes:
        print(f"  note: {note}")


def print_payouts(report) -> None:
    heading("5. FARMER PAYOUT SPLIT  (optimised plan)")
    payout = report.optimized_payouts
    print(
        f"  {'farmer':24} {'crop':13} {'kg':>6} {'truck':>6} {'INR/kg':>7} "
        f"{'gross':>10} {'transport':>10} {'net':>10} {'middleman':>10} {'gain':>10} {'gain%':>7}"
    )
    print("  " + "-" * (WIDTH - 4))
    for row in sorted(payout.farmers, key=lambda r: r.farmer_id):
        gain_pct = "n/a" if row.gain_pct is None else f"{row.gain_pct:+.0f}%"
        print(
            f"  {row.farmer_id + ' ' + row.name:24.24} {row.crop:13} {row.kg:>6,} "
            f"{row.truck_id:>6} {row.price_per_kg:>7.2f} {row.gross_revenue:>10,.0f} "
            f"{row.transport_share:>10,.0f} {row.net_payout:>10,.0f} "
            f"{row.middleman_revenue:>10,.0f} {row.gain_vs_middleman:>10,.0f} {gain_pct:>7}"
        )
    print("  " + "-" * (WIDTH - 4))
    print(
        f"  TOTAL gross INR {payout.total_gross:,.0f} - transport INR {payout.total_transport:,.0f} "
        f"= net INR {payout.total_net:,.0f}"
    )
    print(
        f"  At middleman prices those same lots pay INR {payout.total_middleman:,.0f}, "
        f"so farmers gain INR {payout.total_gain:,.0f}"
        + (f" (avg {payout.avg_gain_pct:+.0f}% per farmer)." if payout.avg_gain_pct is not None else ".")
    )
    baseline_payout = report.baseline_payouts
    print(
        f"  Greedy plan pays farmers INR {baseline_payout.total_net:,.0f} net "
        f"(INR {baseline_payout.total_net_after_spoilage:,.0f} once spoiled lots are written off, "
        f"vs INR {payout.total_net_after_spoilage:,.0f} optimised)."
    )


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def verify_dataset(checks: Checks, dataset) -> None:
    rebuilt = build_dataset()
    checks.check(
        "dataset generation is deterministic (random.seed(42))",
        rebuilt.model_dump_json() == dataset.model_dump_json(),
    )
    checks.check("dataset has 25 farmers", len(dataset.farmers) == 25, f"got {len(dataset.farmers)}")
    checks.check("dataset has 3 buyers", len(dataset.buyers) == 3, f"got {len(dataset.buyers)}")
    checks.check("dataset has 4 trucks", len(dataset.trucks) == 4, f"got {len(dataset.trucks)}")
    checks.check(
        "truck capacities are 800/1200/1200/2000 kg",
        sorted(t.capacity_kg for t in dataset.trucks) == [800, 1200, 1200, 2000],
        f"got {sorted(t.capacity_kg for t in dataset.trucks)}",
    )

    distances = [
        road_distance_m(NAGPUR, Point(f.lat, f.lng)) / 1000.0 / 1.3 for f in dataset.farmers
    ]
    checks.check(
        "every farmer is 5-60 km from Nagpur (straight line)",
        all(5.0 <= d <= 60.0 for d in distances),
        f"range {min(distances):.1f}-{max(distances):.1f} km",
    )
    checks.check(
        "supply slightly exceeds fleet capacity, so the solver must trade off",
        dataset.total_capacity_kg() < dataset.total_supply_kg() <= dataset.total_capacity_kg() * 1.3,
        f"{dataset.total_supply_kg()} kg supply vs {dataset.total_capacity_kg()} kg capacity",
    )
    checks.check(
        "every crop has at least one buyer",
        all(dataset.buyers_for_crop(c) for c in dataset.crops),
        detail=f"orphans: {[c for c in dataset.crops if not dataset.buyers_for_crop(c)]}",
    )


def verify_plan_integrity(checks: Checks, dataset, plan: PlanResult, label: str) -> None:
    """Checks that must hold for *any* plan, optimised or greedy."""
    served = plan.served_farmer_ids()
    checks.check(
        f"{label}: no farmer is picked up twice",
        len(served) == len(set(served)),
        f"{len(served)} stops, {len(set(served))} distinct",
    )
    checks.check(
        f"{label}: served + unserved accounts for every farmer",
        len(served) + len(plan.unserved) == len(dataset.farmers),
        f"{len(served)} served + {len(plan.unserved)} unserved vs {len(dataset.farmers)} farmers",
    )
    overloaded = [
        f"{r.truck_id} {r.load_kg}>{r.truck_capacity_kg}"
        for r in plan.routes
        if r.load_kg > r.truck_capacity_kg
    ]
    checks.check(
        f"{label}: no truck exceeds its capacity", not overloaded, detail="; ".join(overloaded)
    )

    mismatched_legs = [
        r.truck_id
        for r in plan.routes
        if r.distance_m != sum(s.leg_distance_m for s in r.stops)
    ]
    checks.check(
        f"{label}: route distance equals the sum of its legs",
        not mismatched_legs,
        detail=f"inconsistent: {mismatched_legs}",
    )


def verify_optimised_constraints(checks: Checks, dataset, plan: PlanResult) -> None:
    """The hard constraints the OR-Tools model promises to respect."""
    checks.check(
        "optimiser returned a usable solution",
        plan.solver_status in GOOD_STATUSES,
        f"status {plan.solver_status}",
    )
    checks.check("optimiser produced at least one route", bool(plan.routes))

    late_pickups = []
    outside_window = []
    wrong_crop = []
    for route in plan.routes:
        buyer = dataset.buyer_by_id(route.buyer_id) if route.buyer_id else None
        for stop in route.stops:
            if stop.kind != StopKind.FARMER:
                continue
            farmer = dataset.farmer_by_id(stop.ref_id)
            if farmer is None:
                continue
            lower, upper = dataset.pickup_window(farmer)
            if stop.minutes_past_deadline > 0:
                late_pickups.append(f"{stop.ref_id} +{stop.minutes_past_deadline}m")
            if not lower <= stop.arrival_min <= upper:
                outside_window.append(f"{stop.ref_id} at {stop.arrival_min} not in [{lower},{upper}]")
            if buyer is not None and not buyer.wants(stop.crop or ""):
                wrong_crop.append(f"{stop.ref_id} {stop.crop} -> {buyer.id}")

    checks.check(
        "optimiser: no lot is picked up after its spoilage deadline",
        not late_pickups,
        detail="; ".join(late_pickups),
    )
    checks.check(
        "optimiser: every pickup falls inside the farmer's pickup window",
        not outside_window,
        detail="; ".join(outside_window),
    )
    checks.check(
        "optimiser: every lot goes to a buyer that wants that crop",
        not wrong_crop,
        detail="; ".join(wrong_crop),
    )

    late_deliveries = []
    for route in plan.routes:
        buyer = dataset.buyer_by_id(route.buyer_id) if route.buyer_id else None
        if buyer and not (buyer.window_start <= route.delivery_min <= buyer.window_end):
            late_deliveries.append(
                f"{route.truck_id} at {route.delivery_min} not in "
                f"[{buyer.window_start},{buyer.window_end}]"
            )
    checks.check(
        "optimiser: every delivery lands inside the buyer's window",
        not late_deliveries,
        detail="; ".join(late_deliveries),
    )

    overdelivered = []
    for buyer in dataset.buyers:
        delivered = sum(r.load_kg for r in plan.routes if r.buyer_id == buyer.id)
        if delivered > buyer.demand_kg:
            overdelivered.append(f"{buyer.id} {delivered}>{buyer.demand_kg}")
    checks.check(
        "optimiser: no buyer is sent more than it asked for",
        not overdelivered,
        detail="; ".join(overdelivered),
    )

    one_truck_once = [
        truck.id
        for truck in dataset.trucks
        if sum(1 for r in plan.routes if r.truck_id == truck.id) > 1
    ]
    checks.check(
        "optimiser: each truck is used for at most one route",
        not one_truck_once,
        detail=f"reused: {one_truck_once}",
    )


def verify_improvement(checks: Checks, report) -> None:
    base, opt = report.baseline, report.optimized
    checks.check(
        "optimiser beats the baseline on total_km",
        opt.total_km < base.total_km,
        f"optimised {opt.total_km:,.2f} km vs baseline {base.total_km:,.2f} km",
    )
    checks.check(
        "optimiser beats the baseline on kg_spoiled",
        opt.kg_spoiled < base.kg_spoiled,
        f"optimised {opt.kg_spoiled:,} kg vs baseline {base.kg_spoiled:,} kg",
    )
    checks.check(
        "optimiser does not cost more than the baseline",
        opt.total_cost <= base.total_cost,
        f"optimised INR {opt.total_cost:,.2f} vs baseline INR {base.total_cost:,.2f}",
    )


def run_disruption(checks: Checks) -> None:
    """Break a truck mid-plan and make sure the re-plan is sane."""
    heading("6. DISRUPTION REPLAY  (truck T4 breaks down)")
    dataset = build_dataset()
    before = solve(dataset, time_limit_seconds=TIME_LIMIT)
    victim = max(dataset.trucks, key=lambda t: t.capacity_kg)
    dataset.trucks.remove(victim)
    after = solve(dataset, time_limit_seconds=TIME_LIMIT)
    diff = metrics.diff_plans(dataset, before, after)

    print(
        f"  Removed {victim.id} ({victim.capacity_kg:,} kg). Fleet capacity is now "
        f"{dataset.total_capacity_kg():,} kg for {dataset.total_supply_kg():,} kg of supply."
    )
    print(
        f"  farmers served {diff.farmers_served_before} -> {diff.farmers_served_after} "
        f"({diff.farmers_served_delta:+d})   "
        f"kg {diff.kg_moved_before:,} -> {diff.kg_moved_after:,} ({diff.kg_moved_delta:+,})   "
        f"km {diff.km_before:,.1f} -> {diff.km_after:,.1f} ({diff.km_delta:+,.1f})   "
        f"cost INR {diff.cost_before:,.0f} -> {diff.cost_after:,.0f} ({diff.cost_delta:+,.0f})"
    )
    if diff.newly_unserved:
        print("  lost their pickup:")
        for change in diff.newly_unserved:
            print(
                f"    {change.farmer_id} {change.name:22} {change.crop:13} "
                f"{change.quantity_kg:>5,} kg  (was on {change.from_truck})"
            )
    if diff.reassigned:
        print("  moved to another truck:")
        for change in diff.reassigned:
            print(
                f"    {change.farmer_id} {change.name:22} {change.from_truck} -> {change.to_truck}"
            )
    for note in diff.notes:
        print(f"  note: {note}")

    checks.check(
        "disruption: re-plan still returns a usable solution",
        after.solver_status in GOOD_STATUSES,
        f"status {after.solver_status}",
    )
    checks.check(
        "disruption: the broken truck is gone from the new plan",
        all(r.truck_id != victim.id for r in after.routes),
    )
    checks.check(
        "disruption: a smaller fleet does not move more produce",
        after.kg_moved <= before.kg_moved,
        f"{after.kg_moved:,} kg after vs {before.kg_moved:,} kg before",
    )
    checks.check(
        "disruption: the diff explains the change",
        bool(diff.newly_unserved or diff.reassigned or diff.notes),
    )
    verify_plan_integrity(checks, dataset, after, "post-disruption")


# --------------------------------------------------------------------------- #
def main() -> int:
    checks = Checks()

    dataset = build_dataset()
    print_dataset(dataset)
    verify_dataset(checks, dataset)

    heading("2. PLANNING")
    print(f"  solver time limit: {TIME_LIMIT:g}s")
    optimized = solve(dataset, time_limit_seconds=TIME_LIMIT)
    print(
        f"  optimiser: {optimized.solver_status} in {optimized.solve_seconds:.3f}s -> "
        f"{optimized.farmers_served} farmers, {optimized.kg_moved:,} kg, "
        f"{optimized.total_distance_km:,.2f} km"
    )
    greedy = baseline.plan(dataset)
    print(
        f"  baseline:  {greedy.solver_status} in {greedy.solve_seconds:.3f}s -> "
        f"{greedy.farmers_served} farmers, {greedy.kg_moved:,} kg, "
        f"{greedy.total_distance_km:,.2f} km"
    )
    report = metrics.compare(dataset, greedy, optimized)

    print_table(report)
    print_routes(dataset, optimized, "4a. OPTIMISED ROUTES (OR-Tools)")
    print_routes(dataset, greedy, "4b. BASELINE ROUTES (naive greedy)")
    print_payouts(report)

    verify_plan_integrity(checks, dataset, optimized, "optimiser")
    verify_plan_integrity(checks, dataset, greedy, "baseline")
    verify_optimised_constraints(checks, dataset, optimized)
    verify_improvement(checks, report)

    run_disruption(checks)

    ok = checks.report()
    print("\n" + ("SMOKE TEST PASSED" if ok else "SMOKE TEST FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
