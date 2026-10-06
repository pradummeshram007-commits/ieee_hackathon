"""KisanRoute HTTP API.

One FastAPI app, one process, state shared by every session through SQLite (or
PostgreSQL when ``DATABASE_URL`` is set).

**Authentication.** Every ``/api`` route except ``/api/auth/login`` and
``/api/auth/register`` needs ``Authorization: Bearer <token>``; ``/health`` is
open so a platform health check works without credentials.

**Authorisation is enforced here and in app/workflow.py, never in the browser.**
A farmer's own view is assembled from their username, so another farmer's
payout is not filtered out of the response -- it was never in it. Endpoints
that take an id check the caller and return 403.

Route groups:

============================  ===============================================
``/health``                   liveness, unauthenticated
``/api/auth/*``               register, login, logout, whoami
``/api/me/state``             the role-scoped payload every view polls
``/api/me/lots``              a farmer lists produce
``/api/me/demand``            a buyer posts demand
``/api/trips/*``              accept / decline / pick up / deliver
``/api/farmers/{id}``         one lot with its payout (self or coordinator)
``/api/dataset|plan|disrupt|reset``   coordinator dashboard, unchanged shapes
``/``                         static frontend
============================  ===============================================
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, baseline, metrics, workflow
from . import db as database
from .models import (
    AuthResponse,
    Buyer,
    BuyerCreate,
    Dataset,
    DatasetResponse,
    DemandUpdate,
    DisruptRequest,
    DisruptResponse,
    DisruptionType,
    Farmer,
    FarmerCreate,
    LoginRequest,
    LotCreate,
    LotPriceUpdate,
    NotificationsRead,
    PlanMode,
    PlanRequest,
    PlanResponse,
    PlanResult,
    RegisterRequest,
    Role,
    UserPublic,
)
from .seed import DEMO_PASSWORD, default_price_for

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_PICKUP_WINDOW_MIN = 360
DEFAULT_PACK_TIME_MIN = 30

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create tables, load or seed the world, and plan once before serving."""
    workflow.bootstrap()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="KisanRoute",
    version=__version__,
    description=(
        "Smallholder produce aggregation and pickup routing, with accounts: farmers "
        "list lots, the platform proposes truck trips, drivers accept them, and every "
        "session sees the same live plan."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Auth plumbing
# --------------------------------------------------------------------------- #
def current_user(token: str = Depends(auth.bearer_token)) -> UserPublic:
    with database.session_scope() as db:
        user = auth.user_for_token(db, token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid or expired token")
        return auth.public_user(user)


def require_coordinator(user: UserPublic = Depends(current_user)) -> UserPublic:
    if user.role != Role.COORDINATOR:
        raise HTTPException(
            status_code=403,
            detail=f"'{user.username}' is a {user.role.value}; this view is coordinator-only",
        )
    return user


def _workflow_call(fn, *args, **kwargs):
    """Run a workflow function, mapping its rule violations onto HTTP codes."""
    try:
        return fn(*args, **kwargs)
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


# --------------------------------------------------------------------------- #
# Health and auth
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["ops"])
def health() -> Dict[str, object]:
    return {"status": "ok", "version": workflow.version(), "planning": workflow.is_planning()}


@app.post("/api/auth/login", response_model=AuthResponse, tags=["auth"])
def login(payload: LoginRequest) -> AuthResponse:
    with database.session_scope() as db:
        user = auth.authenticate(db, payload.username, payload.password)
        if user is None:
            raise HTTPException(status_code=401, detail="unknown username or wrong password")
        token = auth.issue_token(db, user.username)
        return AuthResponse(token=token, user=auth.public_user(user))


@app.post("/api/auth/register", response_model=AuthResponse, status_code=201, tags=["auth"])
def register(payload: RegisterRequest) -> AuthResponse:
    """Farmer self-registration. The farm location comes from a map click."""
    username = payload.username.strip().lower()
    with database.session_scope() as db:
        if database.get_user(db, username) is not None:
            raise HTTPException(status_code=409, detail=f"username '{username}' is taken")
        user = auth.create_user(
            db,
            username=username,
            password=payload.password,
            role=Role.FARMER.value,
            ref_id=None,          # lots are created later and carry the link
            display_name=payload.name.strip(),
            village=payload.village.strip(),
            phone=payload.phone.strip(),
            lat=payload.lat,
            lng=payload.lng,
        )
        token = auth.issue_token(db, user.username)
        return AuthResponse(token=token, user=auth.public_user(user))


@app.post("/api/auth/logout", tags=["auth"])
def logout(token: str = Depends(auth.bearer_token)) -> Dict[str, object]:
    with database.session_scope() as db:
        return {"status": "logged_out", "existed": auth.revoke_token(db, token)}


@app.get("/api/auth/me", response_model=UserPublic, tags=["auth"])
def whoami(user: UserPublic = Depends(current_user)) -> UserPublic:
    return user


@app.get("/api/auth/demo-accounts", tags=["auth"])
def demo_account_hints() -> Dict[str, object]:
    """The one-click chips on the login screen. Demo scaffolding, not secrets."""
    with workflow.read() as state:
        farmer = next((f for f in state.dataset.farmers if f.owner == "f02"), None)
        buyer = state.dataset.buyer_by_id("B1")
        return {
            "password": DEMO_PASSWORD,
            "chips": [
                {"username": "kiran", "role": "farmer", "label": "Kiran (new farmer)"},
                {"username": "f02", "role": "farmer",
                 "label": f"{farmer.name.split()[0]} (farmer)" if farmer else "Farmer"},
                {"username": "t1", "role": "driver", "label": "T1"},
                {"username": "t2", "role": "driver", "label": "T2"},
                {"username": "t3", "role": "driver", "label": "T3"},
                {"username": "t4", "role": "driver", "label": "T4"},
                {"username": "b1", "role": "buyer",
                 "label": f"{buyer.name.split()[0]} Retail (buyer)" if buyer else "Buyer"},
                {"username": "admin", "role": "coordinator", "label": "Coordinator"},
            ],
        }


# --------------------------------------------------------------------------- #
# The live, role-scoped state every view polls
# --------------------------------------------------------------------------- #
@app.get("/api/me/state", tags=["live"])
def me_state(
    since: Optional[int] = Query(default=None, ge=0),
    user: UserPublic = Depends(current_user),
) -> Dict[str, object]:
    """Poll target. Returns ``changed: false`` and no payload when nothing moved."""
    return _workflow_call(workflow.state_envelope, user, since)


@app.post("/api/me/lots", status_code=201, tags=["live"])
def post_lot(payload: LotCreate, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    lot = _workflow_call(workflow.post_lot, user, payload)
    return {"lot": lot, "version": workflow.version(), "planning": workflow.is_planning()}


@app.post("/api/me/demand", tags=["live"])
def post_demand(payload: DemandUpdate, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    buyer = _workflow_call(workflow.set_demand, user, payload)
    return {"buyer": buyer, "version": workflow.version(), "planning": workflow.is_planning()}


@app.post("/api/me/lots/{lot_id}/accept", tags=["live"])
def accept_match(lot_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    """The farmer takes their match. The trip confirms once everyone has."""
    result = _workflow_call(workflow.accept_match, user, lot_id.upper())
    return {**result, "version": workflow.version()}


@app.post("/api/me/lots/{lot_id}/decline", tags=["live"])
def decline_match(lot_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    """Turn the match down: the lot is withdrawn and the rest is re-planned."""
    result = _workflow_call(workflow.decline_match, user, lot_id.upper())
    return {**result, "version": workflow.version(), "planning": workflow.is_planning()}


@app.post("/api/me/lots/{lot_id}/price", tags=["live"])
def set_lot_price(
    lot_id: str, payload: LotPriceUpdate, user: UserPublic = Depends(current_user)
) -> Dict[str, object]:
    """Edit price. A lower floor can open up buyers, so this re-plans."""
    lot = _workflow_call(workflow.set_lot_price, user, lot_id.upper(), payload.min_price_per_kg)
    return {"lot": lot, "version": workflow.version(), "planning": workflow.is_planning()}


@app.post("/api/me/notifications/read", tags=["live"])
def read_notifications(
    payload: NotificationsRead = Body(default=NotificationsRead()),
    user: UserPublic = Depends(current_user),
) -> Dict[str, object]:
    marked = _workflow_call(workflow.mark_notifications_read, user, payload.ids)
    return {"marked": marked, "version": workflow.version()}


# --------------------------------------------------------------------------- #
# Trips
# --------------------------------------------------------------------------- #
@app.get("/api/trips/{truck_id}", tags=["trips"])
def get_trip(truck_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    return _workflow_call(workflow.trip_detail, user, truck_id.upper())


@app.post("/api/trips/{truck_id}/accept", tags=["trips"])
def accept(truck_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    trip = _workflow_call(workflow.accept_trip, user, truck_id.upper())
    return {"trip": trip, "version": workflow.version()}


@app.post("/api/trips/{truck_id}/decline", tags=["trips"])
def decline(truck_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    trip = _workflow_call(workflow.decline_trip, user, truck_id.upper())
    return {"trip": trip, "version": workflow.version(), "planning": workflow.is_planning()}


@app.post("/api/trips/{truck_id}/stops/{farmer_id}/pickup", tags=["trips"])
def pickup(truck_id: str, farmer_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    trip = _workflow_call(workflow.mark_picked_up, user, truck_id.upper(), farmer_id.upper())
    return {"trip": trip, "version": workflow.version()}


@app.post("/api/trips/{truck_id}/deliver", tags=["trips"])
def deliver(truck_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    trip = _workflow_call(workflow.mark_delivered, user, truck_id.upper())
    return {"trip": trip, "version": workflow.version()}


@app.get("/api/farmers/{farmer_id}", tags=["dataset"])
def get_farmer(farmer_id: str, user: UserPublic = Depends(current_user)) -> Dict[str, object]:
    """One lot with its payout. Self or coordinator only -- 403 for anyone else."""
    return _workflow_call(workflow.farmer_detail, user, farmer_id.upper())


# --------------------------------------------------------------------------- #
# Coordinator dashboard (unchanged response shapes)
# --------------------------------------------------------------------------- #
@app.get("/api/dataset", response_model=DatasetResponse, tags=["dataset"])
def get_dataset(user: UserPublic = Depends(require_coordinator)) -> DatasetResponse:
    with workflow.read() as state:
        return DatasetResponse(
            summary={
                **state.dataset.summary(),
                "seed": state.seed,
                "disruptions_applied": list(state.disruptions),
                "version": workflow.version(),
            },
            crops=state.dataset.crops,
            farmers=state.dataset.farmers,
            buyers=state.dataset.buyers,
            trucks=state.dataset.trucks,
        )


def _next_id(existing: List[str], prefix: str, width: int) -> str:
    used = set(existing)
    n = 1
    while f"{prefix}{n:0{width}d}" in used:
        n += 1
    return f"{prefix}{n:0{width}d}"


@app.post("/api/farmers", status_code=201, tags=["dataset"])
def add_farmer(
    payload: FarmerCreate, user: UserPublic = Depends(require_coordinator)
) -> Dict[str, object]:
    """Coordinator tool: add a lot directly, without an owning account."""
    with workflow.mutate() as state:
        dataset = state.dataset
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
            payload.ready_from if payload.ready_from is not None
            else harvest_time + DEFAULT_PACK_TIME_MIN
        )
        ready_until = (
            payload.ready_until if payload.ready_until is not None
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
        summary = dataset.summary()
        deadline = dataset.spoilage_deadline(farmer)
        window = list(dataset.pickup_window(farmer))

    workflow.replan()
    return {
        "farmer": farmer,
        "spoilage_deadline_min": deadline,
        "pickup_window": window,
        "dataset": summary,
    }


@app.post("/api/buyers", status_code=201, tags=["dataset"])
def add_buyer(
    payload: BuyerCreate, user: UserPublic = Depends(require_coordinator)
) -> Dict[str, object]:
    """Coordinator tool: add a buyer. Prices default to the type's premium."""
    with workflow.mutate() as state:
        dataset = state.dataset
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
        summary = dataset.summary()

    workflow.replan()
    return {"buyer": buyer, "dataset": summary}


@app.post("/api/plan", response_model=PlanResponse, tags=["planning"])
def create_plan(
    payload: PlanRequest = Body(default=PlanRequest()),
    user: UserPublic = Depends(require_coordinator),
) -> PlanResponse:
    """Plan now, synchronously, honouring every accepted trip.

    The baseline is deliberately *not* lock-aware: it is the "what if nobody
    optimised anything" control, so it always replans the whole day from
    scratch. That keeps the comparison meaningful rather than flattering.
    """
    if payload.mode in (PlanMode.OPTIMIZED, PlanMode.BOTH):
        workflow.replan(blocking=True, time_limit=payload.time_limit_seconds)

    with workflow.read() as state:
        optimized: Optional[PlanResult] = (
            state.plan if payload.mode in (PlanMode.OPTIMIZED, PlanMode.BOTH) else None
        )
        greedy: Optional[PlanResult] = (
            (state.baseline or baseline.plan(state.dataset))
            if payload.mode in (PlanMode.BASELINE, PlanMode.BOTH)
            else None
        )
        comparison = (
            metrics.compare(state.dataset, greedy, optimized)
            if optimized is not None and greedy is not None
            else None
        )
        return PlanResponse(
            mode=payload.mode.value,
            dataset={**state.dataset.summary(), "version": workflow.version()},
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
    orphaned = [f.id for f in dataset.farmers if not dataset.buyers_for_crop(f.crop)]
    detail = (
        f" {len(orphaned)} lot(s) now have no outlet at all ({', '.join(orphaned)})."
        if orphaned else ""
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
        raise HTTPException(status_code=400, detail="pickup_delay needs a positive delay_minutes")

    old_window = dataset.pickup_window(farmer)
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
def disrupt(
    payload: DisruptRequest, user: UserPublic = Depends(require_coordinator)
) -> DisruptResponse:
    """Apply a disruption, re-plan, and diff against the plan that preceded it."""
    with workflow.read() as state:
        before = state.plan
    if before is None:
        workflow.replan(blocking=True, time_limit=payload.time_limit_seconds)
        with workflow.read() as state:
            before = state.plan

    with workflow.mutate() as state:
        dataset = state.dataset
        if payload.type == DisruptionType.TRUCK_BREAKDOWN:
            description = _apply_truck_breakdown(dataset, payload.target_id)
        elif payload.type == DisruptionType.BUYER_CANCEL:
            description = _apply_buyer_cancel(dataset, payload.target_id)
        else:
            description = _apply_pickup_delay(dataset, payload.target_id, payload.delay_minutes)
        state.disruptions.append(f"{payload.type.value}:{payload.target_id}")

    workflow.replan(blocking=True, time_limit=payload.time_limit_seconds)

    with workflow.read() as state:
        after = state.plan
        greedy = state.baseline or baseline.plan(state.dataset)
        assert after is not None and before is not None
        return DisruptResponse(
            disruption=payload.type.value,
            target_id=payload.target_id,
            description=description,
            dataset={**state.dataset.summary(), "version": workflow.version()},
            plan=after,
            baseline=greedy,
            comparison=metrics.compare(state.dataset, greedy, after),
            diff=metrics.diff_plans(state.dataset, before, after),
        )


@app.post("/api/reset", tags=["dataset"])
def reset(
    seed: Optional[int] = Body(default=None, embed=True),
    user: UserPublic = Depends(require_coordinator),
) -> Dict[str, object]:
    """Re-seed the world: fresh dataset, no trips, no disruptions. Accounts stay."""
    from .seed import build_dataset

    with workflow.mutate() as state:
        state.seed = state.seed if seed is None else seed
        state.dataset = build_dataset(state.seed)
        state.plan = None
        state.baseline = None
        state.trips = {}
        # Notifications describe trips in the old world; after a re-seed they
        # point at deals that no longer exist.
        state.notifications = {}
        state.disruptions = []
        state.last_planned_at = None
        summary = state.dataset.summary()
        used_seed = state.seed

    workflow.replan()
    return {
        "status": "reset",
        "seed": used_seed,
        "dataset": summary,
        "version": workflow.version(),
    }


# --------------------------------------------------------------------------- #
# Static frontend -- mounted last so every API route above wins.
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
