"""KisanRoute HTTP API.

One FastAPI app, one process, all state in memory. Endpoints:

===========================  ==========================================
``GET  /health``             liveness probe
``GET  /api/dataset``        current farmers, buyers, trucks, crops
``POST /api/farmers``        add a farmer
``POST /api/buyers``         add a buyer
``POST /api/plan``           plan with the optimiser, the greedy baseline, or both
``POST /api/disrupt``        mutate state, re-plan, and diff against the old plan
``POST /api/reset``          rebuild the seed dataset
``GET  /``                   static frontend from ``app/static``
===========================  ==========================================

Concurrency: the planners are CPU-bound and the state is a plain Python
object, so the planning endpoints are declared ``def`` (Starlette runs them in
its thread pool, keeping the event loop responsive) and every read-modify-plan
sequence is taken under a single process-wide lock. Run with one worker; the
state is per-process by design.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__, baseline, metrics
from .models import (
    Buyer,
    BuyerCreate,
    Dataset,
    DatasetResponse,
    DisruptRequest,
    DisruptResponse,
    DisruptionType,
    Farmer,
    FarmerCreate,
    PlanMode,
    PlanRequest,
    PlanResponse,
    PlanResult,
)
from .seed import DEFAULT_SEED, build_dataset, default_price_for
from .solver import solve

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Default pickup-window length for a farmer added through the API without one.
DEFAULT_PICKUP_WINDOW_MIN = 360
DEFAULT_PACK_TIME_MIN = 30


class AppState:
    """Everything the service remembers between requests."""

    def __init__(self, seed: int = DEFAULT_SEED) -> None:
        self.seed = seed
        self.dataset: Dataset = build_dataset(seed)
        self.last_optimized: Optional[PlanResult] = None
        self.last_baseline: Optional[PlanResult] = None
        self.disruptions: List[str] = []

    def reset(self, seed: Optional[int] = None) -> None:
        self.seed = self.seed if seed is None else seed
        self.dataset = build_dataset(self.seed)
        self.last_optimized = None
        self.last_baseline = None
        self.disruptions = []

    def invalidate_plans(self) -> None:
        """Called whenever the dataset changes under a cached plan."""
        self.last_optimized = None
        self.last_baseline = None


STATE = AppState()
STATE_LOCK = threading.Lock()

app = FastAPI(
    title="KisanRoute",
    version=__version__,
    description=(
        "Smallholder produce aggregation and pickup routing: groups nearby farmers "
        "into truck routes that respect truck capacity, per-crop spoilage deadlines "
        "and buyer delivery windows."
    ),
)

# Open CORS: the frontend is served from the same origin in production but from
# a dev server during the hackathon.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Health and dataset
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["ops"])
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dataset", response_model=DatasetResponse, tags=["dataset"])
def get_dataset() -> DatasetResponse:
    with STATE_LOCK:
        dataset = STATE.dataset
        return DatasetResponse(
            summary={
                **dataset.summary(),
                "seed": STATE.seed,
                "disruptions_applied": list(STATE.disruptions),
            },
            crops=dataset.crops,
            farmers=dataset.farmers,
            buyers=dataset.buyers,
            trucks=dataset.trucks,
        )


def _next_id(existing: List[str], prefix: str, width: int) -> str:
    used = set(existing)
    n = 1
    while f"{prefix}{n:0{width}d}" in used:
        n += 1
    return f"{prefix}{n:0{width}d}"


@app.post("/api/farmers", status_code=201, tags=["dataset"])
def add_farmer(payload: FarmerCreate) -> Dict[str, object]:
    """Add a farmer. Harvest and pickup times default to "ready now"."""
    with STATE_LOCK:
        dataset = STATE.dataset
        if payload.crop not in dataset.crops:
            raise HTTPException(
                status_code=400,
                detail=f"unknown crop '{payload.crop}'; known crops: {sorted(dataset.crops)}",
            )
        farmer_id = payload.id or _next_id([f.id for f in dataset.farmers], "F", 2)
        if dataset.farmer_by_id(farmer_id) is not None:
            raise HTTPException(status_code=409, detail=f"farmer '{farmer_id}' already exists")

        harvest_time = payload.harvest_time if payload.harvest_time is not None else 0
        ready_from = (
            payload.ready_from
            if payload.ready_from is not None
            else harvest_time + DEFAULT_PACK_TIME_MIN
        )
        ready_until = (
            payload.ready_until
            if payload.ready_until is not None
            else ready_from + DEFAULT_PICKUP_WINDOW_MIN
        )
        if ready_until < ready_from:
            raise HTTPException(
                status_code=400,
                detail=f"ready_until ({ready_until}) precedes ready_from ({ready_from})",
            )

        farmer = Farmer(
            id=farmer_id,
            name=payload.name,
            village=payload.village,
            lat=payload.lat,
            lng=payload.lng,
            crop=payload.crop,
            quantity_kg=payload.quantity_kg,
            harvest_time=harvest_time,
            ready_from=ready_from,
            ready_until=ready_until,
        )
        dataset.farmers.append(farmer)
        STATE.invalidate_plans()
        return {
            "farmer": farmer,
            "spoilage_deadline_min": dataset.spoilage_deadline(farmer),
            "pickup_window": list(dataset.pickup_window(farmer)),
            "dataset": dataset.summary(),
        }


@app.post("/api/buyers", status_code=201, tags=["dataset"])
def add_buyer(payload: BuyerCreate) -> Dict[str, object]:
    """Add a buyer. Prices default to the usual premium for the buyer type."""
    with STATE_LOCK:
        dataset = STATE.dataset
        unknown = [c for c in payload.crops_wanted if c not in dataset.crops]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown crops {unknown}; known crops: {sorted(dataset.crops)}",
            )
        buyer_id = payload.id or _next_id([b.id for b in dataset.buyers], "B", 1)
        if dataset.buyer_by_id(buyer_id) is not None:
            raise HTTPException(status_code=409, detail=f"buyer '{buyer_id}' already exists")
        if payload.window_end <= payload.window_start:
            raise HTTPException(
                status_code=400,
                detail=f"window_end ({payload.window_end}) must exceed "
                f"window_start ({payload.window_start})",
            )

        if isinstance(payload.price_per_kg, dict):
            prices = {k: float(v) for k, v in payload.price_per_kg.items()}
        elif payload.price_per_kg is not None:
            prices = {c: float(payload.price_per_kg) for c in payload.crops_wanted}
        else:
            prices = {
                c: default_price_for(dataset.crops[c], payload.type) for c in payload.crops_wanted
            }
        # Any wanted crop left without a price gets the type's default premium.
        for crop_name in payload.crops_wanted:
            prices.setdefault(crop_name, default_price_for(dataset.crops[crop_name], payload.type))

        buyer = Buyer(
            id=buyer_id,
            name=payload.name,
            type=payload.type,
            lat=payload.lat,
            lng=payload.lng,
            crops_wanted=payload.crops_wanted,
            price_per_kg=prices,
            demand_kg=payload.demand_kg,
            window_start=payload.window_start,
            window_end=payload.window_end,
        )
        dataset.buyers.append(buyer)
        STATE.invalidate_plans()
        return {"buyer": buyer, "dataset": dataset.summary()}


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _run_plans(dataset: Dataset, mode: PlanMode, time_limit_seconds: float):
    """Run whichever planners ``mode`` asks for. Returns (optimized, baseline)."""
    optimized = (
        solve(dataset, time_limit_seconds=time_limit_seconds)
        if mode in (PlanMode.OPTIMIZED, PlanMode.BOTH)
        else None
    )
    greedy = baseline.plan(dataset) if mode in (PlanMode.BASELINE, PlanMode.BOTH) else None
    return optimized, greedy


@app.post("/api/plan", response_model=PlanResponse, tags=["planning"])
def create_plan(payload: PlanRequest = Body(default=PlanRequest())) -> PlanResponse:
    """Plan pickups. ``mode="both"`` also returns the full metric comparison."""
    with STATE_LOCK:
        dataset = STATE.dataset
        optimized, greedy = _run_plans(dataset, payload.mode, payload.time_limit_seconds)
        if optimized is not None:
            STATE.last_optimized = optimized
        if greedy is not None:
            STATE.last_baseline = greedy

        comparison = (
            metrics.compare(dataset, greedy, optimized)
            if optimized is not None and greedy is not None
            else None
        )
        return PlanResponse(
            mode=payload.mode.value,
            dataset=dataset.summary(),
            optimized=optimized,
            baseline=greedy,
            comparison=comparison,
        )


# --------------------------------------------------------------------------- #
# Disruption
# --------------------------------------------------------------------------- #
def _apply_truck_breakdown(dataset: Dataset, target_id: str) -> str:
    truck = dataset.truck_by_id(target_id)
    if truck is None:
        raise HTTPException(status_code=404, detail=f"no truck '{target_id}'")
    if len(dataset.trucks) == 1:
        raise HTTPException(
            status_code=409,
            detail="cannot break down the only truck in the fleet; add another truck first",
        )
    dataset.trucks.remove(truck)
    return (
        f"Truck {truck.id} ({truck.capacity_kg} kg) broke down and left the fleet; "
        f"remaining capacity {dataset.total_capacity_kg()} kg against "
        f"{dataset.total_supply_kg()} kg of listed supply."
    )


def _apply_buyer_cancel(dataset: Dataset, target_id: str) -> str:
    buyer = dataset.buyer_by_id(target_id)
    if buyer is None:
        raise HTTPException(status_code=404, detail=f"no buyer '{target_id}'")
    if len(dataset.buyers) == 1:
        raise HTTPException(
            status_code=409,
            detail="cannot cancel the only buyer; nothing could be delivered anywhere",
        )
    dataset.buyers.remove(buyer)
    orphaned = [
        f.id for f in dataset.farmers if not dataset.buyers_for_crop(f.crop)
    ]
    detail = (
        f" {len(orphaned)} lot(s) now have no outlet at all ({', '.join(orphaned)})."
        if orphaned
        else ""
    )
    return (
        f"Buyer {buyer.id} ({buyer.name}) cancelled its order for "
        f"{', '.join(buyer.crops_wanted)}; remaining demand {dataset.total_demand_kg()} kg.{detail}"
    )


def _apply_pickup_delay(dataset: Dataset, target_id: str, delay_minutes: Optional[int]) -> str:
    farmer = dataset.farmer_by_id(target_id)
    if farmer is None:
        raise HTTPException(status_code=404, detail=f"no farmer '{target_id}'")
    if not delay_minutes:
        raise HTTPException(
            status_code=400, detail="pickup_delay needs a positive delay_minutes"
        )

    old_window = dataset.pickup_window(farmer)
    # The harvest itself slips, so the lot's whole timeline moves: it is cut
    # later, it is ready later, and its spoilage clock starts later. What does
    # *not* move is the buyer's delivery window, which is what makes this bite.
    farmer.harvest_time += delay_minutes
    farmer.ready_from += delay_minutes
    farmer.ready_until += delay_minutes
    new_window = dataset.pickup_window(farmer)
    return (
        f"Harvest for {farmer.id} ({farmer.name}, {farmer.crop}, {farmer.quantity_kg} kg) "
        f"slipped by {delay_minutes} min: pickup window moved from "
        f"{list(old_window)} to {list(new_window)} while buyer delivery windows stayed put."
    )


@app.post("/api/disrupt", response_model=DisruptResponse, tags=["planning"])
def disrupt(payload: DisruptRequest) -> DisruptResponse:
    """Apply a disruption, re-plan, and return the new plan plus a diff.

    The "before" plan is the last optimised plan if one is cached; otherwise it
    is computed on the pre-disruption state first, so a diff is always
    meaningful even if this is the first call after a reset.
    """
    with STATE_LOCK:
        dataset = STATE.dataset
        before = STATE.last_optimized or solve(
            dataset, time_limit_seconds=payload.time_limit_seconds
        )

        if payload.type == DisruptionType.TRUCK_BREAKDOWN:
            description = _apply_truck_breakdown(dataset, payload.target_id)
        elif payload.type == DisruptionType.BUYER_CANCEL:
            description = _apply_buyer_cancel(dataset, payload.target_id)
        else:
            description = _apply_pickup_delay(dataset, payload.target_id, payload.delay_minutes)

        after = solve(dataset, time_limit_seconds=payload.time_limit_seconds)
        greedy = baseline.plan(dataset)
        STATE.last_optimized = after
        STATE.last_baseline = greedy
        STATE.disruptions.append(f"{payload.type.value}:{payload.target_id}")

        return DisruptResponse(
            disruption=payload.type.value,
            target_id=payload.target_id,
            description=description,
            dataset=dataset.summary(),
            plan=after,
            baseline=greedy,
            comparison=metrics.compare(dataset, greedy, after),
            diff=metrics.diff_plans(dataset, before, after),
        )


@app.post("/api/reset", tags=["dataset"])
def reset(seed: Optional[int] = Body(default=None, embed=True)) -> Dict[str, object]:
    """Re-seed the dataset and drop every cached plan."""
    with STATE_LOCK:
        STATE.reset(seed)
        return {
            "status": "reset",
            "seed": STATE.seed,
            "dataset": STATE.dataset.summary(),
        }


# --------------------------------------------------------------------------- #
# Static frontend -- mounted last so the API routes above always win.
# The directory is created if missing and an empty one is fine: "/" simply
# 404s until a frontend is dropped in.
# --------------------------------------------------------------------------- #
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
