"""Metric roll-ups, solver-vs-baseline comparison, and farmer payout split.

Three things live here.

**Metrics** (:func:`summarise`) reduce a :class:`~app.models.PlanResult` to the
numbers a judge can read in ten seconds: kilometres, cost, farmers served, kg
moved, and -- the one that matters for perishables -- how much produce was
picked up *after* its spoilage deadline.

**Comparison** (:func:`compare`) puts the greedy baseline and the optimiser
side by side, with the improvement signed so that **positive always means the
optimiser did better**, whichever direction the underlying metric runs in.

**Payouts** (:func:`payouts`) split each truck's economics back to the farmers
riding on it:

    revenue        = kg x the buyer's price for that crop
    transport      = that truck's total cost, pro-rated by the farmer's kg
    net payout     = revenue - transport
    middleman      = kg x base price x 0.65   (what they get today)
    gain           = net payout - middleman

A lot whose crop the receiving buyer does not want (only the greedy planner
does this) is valued at the middleman price, because that is where it would
actually end up. Spoiled kilos are written down separately in
``net_payout_after_spoilage`` rather than inside ``net_payout``, so the
headline payout formula stays exactly the one above.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import (
    ComparisonReport,
    Dataset,
    FarmerPayout,
    MetricDelta,
    PayoutSummary,
    PlanDiff,
    PlanMetrics,
    PlanResult,
    RouteChange,
    StopKind,
    MIDDLEMAN_PRICE_FACTOR,
)

LOWER_IS_BETTER = "lower_is_better"
HIGHER_IS_BETTER = "higher_is_better"


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


# --------------------------------------------------------------------------- #
# Payouts
# --------------------------------------------------------------------------- #
def payouts(dataset: Dataset, plan: PlanResult) -> PayoutSummary:
    """Per-farmer economics for one plan."""
    rows: List[FarmerPayout] = []

    for route in plan.routes:
        buyer = dataset.buyer_by_id(route.buyer_id) if route.buyer_id else None
        farmer_stops = [s for s in route.stops if s.kind == StopKind.FARMER]
        route_kg = sum(s.quantity_kg for s in farmer_stops)
        if route_kg <= 0:
            continue

        for stop in farmer_stops:
            crop = dataset.crops.get(stop.crop or "")
            base_price = crop.base_price_per_kg if crop else 0.0
            middleman_price = round(base_price * MIDDLEMAN_PRICE_FACTOR, 2)

            if buyer is not None and stop.crop_accepted and buyer.wants(stop.crop or ""):
                price = buyer.price_for(stop.crop or "")
            else:
                # Nobody at the gate wants it: it goes back to the aggregator.
                price = middleman_price

            kg = stop.quantity_kg
            gross = round(kg * price, 2)
            transport_share = round(route.cost * _safe_div(kg, route_kg), 2)
            net = round(gross - transport_share, 2)
            middleman_revenue = round(kg * middleman_price, 2)
            gain = round(net - middleman_revenue, 2)
            spoiled = stop.minutes_past_deadline > 0
            net_after_spoilage = round(0.0 - transport_share, 2) if spoiled else net

            rows.append(
                FarmerPayout(
                    farmer_id=stop.ref_id,
                    name=stop.name,
                    village=stop.village or "",
                    crop=stop.crop or "",
                    kg=kg,
                    truck_id=route.truck_id,
                    buyer_id=route.buyer_id,
                    buyer_name=route.buyer_name,
                    price_per_kg=round(price, 2),
                    gross_revenue=gross,
                    transport_share=transport_share,
                    net_payout=net,
                    net_price_per_kg=round(_safe_div(net, kg), 2),
                    middleman_price_per_kg=middleman_price,
                    middleman_revenue=middleman_revenue,
                    gain_vs_middleman=gain,
                    gain_pct=round(100.0 * gain / middleman_revenue, 1)
                    if middleman_revenue
                    else None,
                    spoiled=spoiled,
                    crop_accepted=stop.crop_accepted,
                    net_payout_after_spoilage=net_after_spoilage,
                )
            )

    gains = [r.gain_pct for r in rows if r.gain_pct is not None]
    return PayoutSummary(
        mode=plan.mode,
        farmers=rows,
        total_gross=round(sum(r.gross_revenue for r in rows), 2),
        total_transport=round(sum(r.transport_share for r in rows), 2),
        total_net=round(sum(r.net_payout for r in rows), 2),
        total_net_after_spoilage=round(sum(r.net_payout_after_spoilage for r in rows), 2),
        total_middleman=round(sum(r.middleman_revenue for r in rows), 2),
        total_gain=round(sum(r.gain_vs_middleman for r in rows), 2),
        avg_gain_pct=round(sum(gains) / len(gains), 1) if gains else None,
    )


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def summarise(dataset: Dataset, plan: PlanResult, payout: Optional[PayoutSummary] = None) -> PlanMetrics:
    """Reduce a plan to comparable scalars."""
    if payout is None:
        payout = payouts(dataset, plan)

    farmer_stops = [s for route in plan.routes for s in route.stops if s.kind == StopKind.FARMER]

    # Spoilage: hours past the deadline at pickup, weighted by kilos.
    spoilage_risk_score = round(
        sum((s.minutes_past_deadline / 60.0) * s.quantity_kg for s in farmer_stops), 2
    )
    kg_spoiled = sum(s.quantity_kg for s in farmer_stops if s.minutes_past_deadline > 0)
    on_time = sum(1 for s in farmer_stops if s.minutes_past_deadline == 0)

    # Produce that actually reaches a paying buyer in sound condition: on time,
    # and to a buyer that wants the crop at the farmer's price. Raw kg_moved
    # flatters any planner that collects produce it cannot sell.
    kg_sellable = sum(
        s.quantity_kg for s in farmer_stops
        if s.minutes_past_deadline == 0 and s.crop_accepted
    )

    fleet_capacity = dataset.total_capacity_kg()
    routes_on_time = sum(1 for r in plan.routes if r.delivery_on_time)

    return PlanMetrics(
        mode=plan.mode,
        total_km=round(plan.total_distance_km, 2),
        total_cost=round(plan.total_cost, 2),
        farmers_served=plan.farmers_served,
        farmers_unserved=len(plan.unserved),
        kg_moved=plan.kg_moved,
        trucks_used=plan.trucks_used,
        capacity_utilisation_pct=round(100.0 * _safe_div(plan.kg_moved, fleet_capacity), 1),
        spoilage_risk_score=spoilage_risk_score,
        kg_spoiled=kg_spoiled,
        kg_spoiled_pct=round(100.0 * _safe_div(kg_spoiled, plan.kg_moved), 1),
        deadlines_met_pct=round(100.0 * _safe_div(on_time, len(farmer_stops)), 1)
        if farmer_stops
        else 100.0,
        buyer_windows_met_pct=round(100.0 * _safe_div(routes_on_time, len(plan.routes)), 1)
        if plan.routes
        else 100.0,
        kg_crop_mismatch=sum(r.kg_rejected for r in plan.routes),
        kg_sellable=kg_sellable,
        km_per_tonne=round(_safe_div(plan.total_distance_km, plan.kg_moved / 1000.0), 2),
        km_per_sellable_tonne=round(
            _safe_div(plan.total_distance_km, kg_sellable / 1000.0), 2
        ),
        cost_per_kg=round(_safe_div(plan.total_cost, plan.kg_moved), 2),
        cost_per_sellable_kg=round(_safe_div(plan.total_cost, kg_sellable), 2),
        farmer_payout_total=payout.total_net,
        farmer_gain_vs_middleman=payout.total_gain,
    )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def _improvement_pct(baseline: float, optimized: float, direction: str) -> Optional[float]:
    """Percentage improvement, signed so positive always favours the optimiser.

    ``None`` when the baseline is zero and the two differ, because "x% better
    than nothing" is not a number worth printing.
    """
    if baseline == optimized:
        return 0.0
    if baseline == 0:
        return None
    if direction == LOWER_IS_BETTER:
        return round(100.0 * (baseline - optimized) / abs(baseline), 1)
    return round(100.0 * (optimized - baseline) / abs(baseline), 1)


def _delta(metric: str, unit: str, baseline: float, optimized: float, direction: str) -> MetricDelta:
    """One comparison row. ``better`` means "the optimiser is no worse"."""
    better = optimized <= baseline if direction == LOWER_IS_BETTER else optimized >= baseline
    return MetricDelta(
        metric=metric,
        unit=unit,
        baseline=round(float(baseline), 2),
        optimized=round(float(optimized), 2),
        delta=round(float(optimized) - float(baseline), 2),
        improvement_pct=_improvement_pct(float(baseline), float(optimized), direction),
        direction=direction,
        better=better,
    )


#: (metric name, unit, attribute on PlanMetrics, direction)
METRIC_SPEC = [
    ("total_km", "km", "total_km", LOWER_IS_BETTER),
    ("total_cost", "INR", "total_cost", LOWER_IS_BETTER),
    ("farmers_served", "farmers", "farmers_served", HIGHER_IS_BETTER),
    ("kg_moved", "kg", "kg_moved", HIGHER_IS_BETTER),
    ("kg_spoiled", "kg", "kg_spoiled", LOWER_IS_BETTER),
    ("spoilage_risk_score", "kg-hours late", "spoilage_risk_score", LOWER_IS_BETTER),
    ("deadlines_met_pct", "%", "deadlines_met_pct", HIGHER_IS_BETTER),
    ("buyer_windows_met_pct", "%", "buyer_windows_met_pct", HIGHER_IS_BETTER),
    ("kg_crop_mismatch", "kg", "kg_crop_mismatch", LOWER_IS_BETTER),
    ("kg_sellable", "kg", "kg_sellable", HIGHER_IS_BETTER),
    ("km_per_sellable_tonne", "km/t", "km_per_sellable_tonne", LOWER_IS_BETTER),
    ("cost_per_sellable_kg", "INR/kg", "cost_per_sellable_kg", LOWER_IS_BETTER),
    ("capacity_utilisation_pct", "%", "capacity_utilisation_pct", HIGHER_IS_BETTER),
    ("km_per_tonne", "km/t", "km_per_tonne", LOWER_IS_BETTER),
    ("cost_per_kg", "INR/kg", "cost_per_kg", LOWER_IS_BETTER),
    ("trucks_used", "trucks", "trucks_used", LOWER_IS_BETTER),
    ("farmer_payout_total", "INR", "farmer_payout_total", HIGHER_IS_BETTER),
    ("farmer_gain_vs_middleman", "INR", "farmer_gain_vs_middleman", HIGHER_IS_BETTER),
]


def compare(dataset: Dataset, baseline_plan: PlanResult, optimized_plan: PlanResult) -> ComparisonReport:
    """Full side-by-side report for the two planners."""
    baseline_payout = payouts(dataset, baseline_plan)
    optimized_payout = payouts(dataset, optimized_plan)
    baseline_metrics = summarise(dataset, baseline_plan, baseline_payout)
    optimized_metrics = summarise(dataset, optimized_plan, optimized_payout)

    deltas = [
        _delta(
            name,
            unit,
            getattr(baseline_metrics, attribute),
            getattr(optimized_metrics, attribute),
            direction,
        )
        for name, unit, attribute, direction in METRIC_SPEC
    ]

    return ComparisonReport(
        baseline=baseline_metrics,
        optimized=optimized_metrics,
        deltas=deltas,
        headline=_headline(baseline_metrics, optimized_metrics),
        optimized_payouts=optimized_payout,
        baseline_payouts=baseline_payout,
    )


def _headline(baseline: PlanMetrics, optimized: PlanMetrics) -> List[str]:
    """A handful of sentences fit for a demo slide."""
    lines: List[str] = []

    # Raw distance compares a feasible plan with an infeasible one, so lead with
    # distance per *sellable* tonne and report the raw figure alongside it.
    sell_gain = _improvement_pct(
        baseline.km_per_sellable_tonne, optimized.km_per_sellable_tonne, LOWER_IS_BETTER
    )
    lines.append(
        f"Distance per sellable tonne: {optimized.km_per_sellable_tonne:,.1f} km/t optimised "
        f"vs {baseline.km_per_sellable_tonne:,.1f} km/t greedy"
        + (f" ({sell_gain:+.1f}% better)" if sell_gain is not None else "")
        + f"  [raw distance {optimized.total_km:,.1f} km vs {baseline.total_km:,.1f} km]"
    )
    lines.append(
        f"Produce that reaches a paying buyer in good condition: "
        f"{optimized.kg_sellable:,} kg vs {baseline.kg_sellable:,} kg"
    )

    cost_gain = _improvement_pct(baseline.total_cost, optimized.total_cost, LOWER_IS_BETTER)
    lines.append(
        f"Transport cost: INR {optimized.total_cost:,.0f} vs INR {baseline.total_cost:,.0f}"
        + (f" ({cost_gain:+.1f}% better)" if cost_gain is not None else "")
    )

    if baseline.kg_spoiled or optimized.kg_spoiled:
        lines.append(
            f"Spoilage: {optimized.kg_spoiled:,} kg picked up late by the optimiser vs "
            f"{baseline.kg_spoiled:,} kg by the greedy planner "
            f"({optimized.spoilage_risk_score:,.1f} vs {baseline.spoilage_risk_score:,.1f} kg-hours past deadline)"
        )
    else:
        lines.append("Spoilage: neither plan picked up a lot after its deadline")

    lines.append(
        f"Deadlines met: {optimized.deadlines_met_pct:.0f}% of optimised pickups vs "
        f"{baseline.deadlines_met_pct:.0f}% of greedy pickups"
    )
    lines.append(
        f"Supply moved: {optimized.kg_moved:,} kg from {optimized.farmers_served} farmers "
        f"vs {baseline.kg_moved:,} kg from {baseline.farmers_served} farmers"
    )
    payout_gain = _improvement_pct(
        baseline.farmer_payout_total, optimized.farmer_payout_total, HIGHER_IS_BETTER
    )
    lines.append(
        f"Farmer net payout: INR {optimized.farmer_payout_total:,.0f} vs "
        f"INR {baseline.farmer_payout_total:,.0f}"
        + (f" ({payout_gain:+.1f}%)" if payout_gain is not None else "")
        + f"; INR {optimized.farmer_gain_vs_middleman:,.0f} above middleman rates"
    )
    return lines


# --------------------------------------------------------------------------- #
# Plan diffing (used by /api/disrupt)
# --------------------------------------------------------------------------- #
def diff_plans(dataset: Dataset, before: PlanResult, after: PlanResult) -> PlanDiff:
    """What changed between two plans of the same dataset."""
    before_truck: Dict[str, str] = {
        fid: route.truck_id for route in before.routes for fid in route.farmer_ids
    }
    after_truck: Dict[str, str] = {
        fid: route.truck_id for route in after.routes for fid in route.farmer_ids
    }

    def describe(farmer_id: str, from_truck: Optional[str], to_truck: Optional[str], change: str) -> RouteChange:
        farmer = dataset.farmer_by_id(farmer_id)
        return RouteChange(
            farmer_id=farmer_id,
            name=farmer.name if farmer else farmer_id,
            crop=farmer.crop if farmer else "",
            quantity_kg=farmer.quantity_kg if farmer else 0,
            from_truck=from_truck,
            to_truck=to_truck,
            change=change,
        )

    newly_unserved = [
        describe(fid, truck, None, "dropped from the plan")
        for fid, truck in sorted(before_truck.items())
        if fid not in after_truck
    ]
    newly_served = [
        describe(fid, None, truck, "added to the plan")
        for fid, truck in sorted(after_truck.items())
        if fid not in before_truck
    ]
    reassigned = [
        describe(fid, before_truck[fid], after_truck[fid], "moved to another truck")
        for fid in sorted(set(before_truck) & set(after_truck))
        if before_truck[fid] != after_truck[fid]
    ]

    notes: List[str] = []
    if not newly_unserved and not newly_served and not reassigned:
        notes.append("Plan is unchanged: the disruption did not bind.")
    if newly_unserved:
        lost_kg = sum(c.quantity_kg for c in newly_unserved)
        notes.append(
            f"{len(newly_unserved)} farmer(s) / {lost_kg} kg lost their pickup and need a "
            "follow-up truck or a price renegotiation."
        )
    if reassigned:
        notes.append(f"{len(reassigned)} farmer(s) were moved to a different truck.")
    if after.farmers_served > before.farmers_served:
        notes.append("Replanning recovered more pickups than the original plan had.")

    return PlanDiff(
        km_before=round(before.total_distance_km, 2),
        km_after=round(after.total_distance_km, 2),
        km_delta=round(after.total_distance_km - before.total_distance_km, 2),
        cost_before=round(before.total_cost, 2),
        cost_after=round(after.total_cost, 2),
        cost_delta=round(after.total_cost - before.total_cost, 2),
        farmers_served_before=before.farmers_served,
        farmers_served_after=after.farmers_served,
        farmers_served_delta=after.farmers_served - before.farmers_served,
        kg_moved_before=before.kg_moved,
        kg_moved_after=after.kg_moved,
        kg_moved_delta=after.kg_moved - before.kg_moved,
        trucks_used_before=before.trucks_used,
        trucks_used_after=after.trucks_used,
        newly_unserved=newly_unserved,
        newly_served=newly_served,
        reassigned=reassigned,
        notes=notes,
    )
