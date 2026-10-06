"""Pydantic schemas for KisanRoute.

Three groups live here:

1. **Domain** -- :class:`Crop`, :class:`Farmer`, :class:`Buyer`,
   :class:`Truck`, :class:`Dataset`.
2. **Results** -- :class:`Stop`, :class:`RouteResult`, :class:`PlanResult` and
   friends.  ``solver.solve()`` and ``baseline.plan()`` both return a
   :class:`PlanResult`, which is what makes them directly comparable.
3. **Metrics / API payloads** -- :class:`PlanMetrics`, :class:`MetricDelta`,
   :class:`FarmerPayout`, :class:`PlanDiff`, and the request bodies.

The time axis is **minutes from plan start**; plan start is 06:00 local, so
``minutes_to_clock(90) == "07:30"``.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Plan start on the wall clock. Only used for human-readable output.
PLAN_START_HOUR = 6

#: What a farmer typically gets from an aggregator/middleman, as a fraction of
#: the crop's base price. The whole point of the project is to beat this.
MIDDLEMAN_PRICE_FACTOR = 0.65


def minutes_to_clock(minutes: Optional[int]) -> str:
    """Render "minutes from plan start" as a 24h clock string."""
    if minutes is None:
        return "--:--"
    total = PLAN_START_HOUR * 60 + int(minutes)
    day = total // 1440
    hh, mm = divmod(total % 1440, 60)
    suffix = f"+{day}d" if day else ""
    return f"{hh:02d}:{mm:02d}{suffix}"


# --------------------------------------------------------------------------- #
# Domain
# --------------------------------------------------------------------------- #
class BuyerType(str, Enum):
    RETAILER = "retailer"
    MANDI = "mandi"
    PROCESSOR = "processor"


class Crop(BaseModel):
    """A crop in the catalogue."""

    model_config = ConfigDict(extra="forbid")

    name: str
    spoilage_hours: float = Field(gt=0, description="Hours from harvest until the lot is a loss")
    base_price_per_kg: float = Field(gt=0, description="Farm-gate reference price, INR/kg")

    @property
    def spoilage_minutes(self) -> int:
        return int(round(self.spoilage_hours * 60))

    @property
    def middleman_price_per_kg(self) -> float:
        return round(self.base_price_per_kg * MIDDLEMAN_PRICE_FACTOR, 2)


class Farmer(BaseModel):
    """A smallholder with one harvested lot ready for pickup."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    village: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    crop: str
    quantity_kg: int = Field(gt=0)
    harvest_time: int = Field(ge=0, description="Minutes from plan start when the lot was cut")
    ready_from: int = Field(ge=0, description="Earliest pickup, minutes from plan start")
    ready_until: int = Field(ge=0, description="Latest the farmer can hand the lot over")

    @field_validator("quantity_kg", "harvest_time", "ready_from", "ready_until", mode="before")
    @classmethod
    def _round_to_int(cls, value: object) -> object:
        """Accept floats from JSON clients; the routing model needs integers."""
        if isinstance(value, float):
            return int(round(value))
        return value

    @model_validator(mode="after")
    def _check_window(self) -> "Farmer":
        if self.ready_until < self.ready_from:
            raise ValueError(
                f"farmer {self.id}: ready_until ({self.ready_until}) precedes "
                f"ready_from ({self.ready_from})"
            )
        return self


class Buyer(BaseModel):
    """Demand side: a retailer, mandi (wholesale market) or processor.

    ``price_per_kg`` is a **premium over the crop's base price**, held per crop
    because a retailer paying a premium for tomato is not paying the same
    premium for potato.  API clients may send either a mapping
    ``{"tomato": 26.1}`` or a single number, which is expanded across
    ``crops_wanted``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: BuyerType
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    crops_wanted: List[str] = Field(min_length=1)
    price_per_kg: Dict[str, float] = Field(default_factory=dict)
    demand_kg: int = Field(gt=0)
    window_start: int = Field(ge=0, description="Delivery window opens, minutes from plan start")
    window_end: int = Field(ge=0, description="Delivery window closes, minutes from plan start")

    @model_validator(mode="before")
    @classmethod
    def _expand_price(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        price = data.get("price_per_kg")
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            crops = data.get("crops_wanted") or []
            data = dict(data)
            data["price_per_kg"] = {str(c): float(price) for c in crops}
        elif isinstance(price, dict):
            data = dict(data)
            data["price_per_kg"] = {str(k): float(v) for k, v in price.items()}
        return data

    @field_validator("demand_kg", "window_start", "window_end", mode="before")
    @classmethod
    def _round_to_int(cls, value: object) -> object:
        if isinstance(value, float):
            return int(round(value))
        return value

    @model_validator(mode="after")
    def _check_window(self) -> "Buyer":
        if self.window_end <= self.window_start:
            raise ValueError(
                f"buyer {self.id}: window_end ({self.window_end}) must exceed "
                f"window_start ({self.window_start})"
            )
        return self

    def wants(self, crop: str) -> bool:
        return crop in self.crops_wanted

    def price_for(self, crop: str) -> float:
        """Price this buyer pays for ``crop``; 0.0 if it does not want it."""
        return float(self.price_per_kg.get(crop, 0.0))


class Truck(BaseModel):
    """A vehicle in the aggregation fleet."""

    model_config = ConfigDict(extra="forbid")

    id: str
    capacity_kg: int = Field(gt=0)
    cost_per_km: float = Field(gt=0, description="All-in running cost, INR/km")
    speed_kmph: float = Field(default=30.0, gt=0)
    depot_lat: float = Field(ge=-90, le=90)
    depot_lng: float = Field(ge=-180, le=180)

    @field_validator("capacity_kg", mode="before")
    @classmethod
    def _round_to_int(cls, value: object) -> object:
        if isinstance(value, float):
            return int(round(value))
        return value


class Dataset(BaseModel):
    """The whole planning world: catalogue, supply, demand, fleet."""

    model_config = ConfigDict(extra="forbid")

    crops: Dict[str, Crop]
    farmers: List[Farmer]
    buyers: List[Buyer]
    trucks: List[Truck]
    plan_start_label: str = f"{PLAN_START_HOUR:02d}:00"
    seed: int = 42

    # -- lookups ----------------------------------------------------------- #
    def farmer_by_id(self, farmer_id: str) -> Optional[Farmer]:
        return next((f for f in self.farmers if f.id == farmer_id), None)

    def buyer_by_id(self, buyer_id: str) -> Optional[Buyer]:
        return next((b for b in self.buyers if b.id == buyer_id), None)

    def truck_by_id(self, truck_id: str) -> Optional[Truck]:
        return next((t for t in self.trucks if t.id == truck_id), None)

    # -- derived domain rules ---------------------------------------------- #
    def spoilage_deadline(self, farmer: Farmer) -> int:
        """Absolute minute after which this lot is a loss.

        ``harvest_time + spoilage_hours * 60``.  An unknown crop is treated as
        "spoils immediately", which makes the farmer visibly infeasible rather
        than silently routed.
        """
        crop = self.crops.get(farmer.crop)
        if crop is None:
            return farmer.harvest_time
        return farmer.harvest_time + crop.spoilage_minutes

    def pickup_window(self, farmer: Farmer) -> tuple[int, int]:
        """The hard pickup window ``[ready_from, min(ready_until, deadline)]``.

        The upper bound may be *below* the lower bound when a lot spoils before
        the farmer can hand it over; callers must treat that as infeasible
        rather than feeding an empty range to OR-Tools.
        """
        return farmer.ready_from, min(farmer.ready_until, self.spoilage_deadline(farmer))

    def buyers_for_crop(self, crop: str) -> List[Buyer]:
        return [b for b in self.buyers if b.wants(crop)]

    def total_supply_kg(self) -> int:
        return sum(f.quantity_kg for f in self.farmers)

    def total_capacity_kg(self) -> int:
        return sum(t.capacity_kg for t in self.trucks)

    def total_demand_kg(self) -> int:
        return sum(b.demand_kg for b in self.buyers)

    def summary(self) -> Dict[str, object]:
        return {
            "farmers": len(self.farmers),
            "buyers": len(self.buyers),
            "trucks": len(self.trucks),
            "crops": sorted(self.crops),
            "villages": sorted({f.village for f in self.farmers}),
            "total_supply_kg": self.total_supply_kg(),
            "total_capacity_kg": self.total_capacity_kg(),
            "total_demand_kg": self.total_demand_kg(),
            "supply_vs_capacity_pct": round(
                100.0 * self.total_supply_kg() / self.total_capacity_kg(), 1
            )
            if self.total_capacity_kg()
            else 0.0,
            "plan_start_label": self.plan_start_label,
        }


# --------------------------------------------------------------------------- #
# Results (identical shape for solver and baseline)
# --------------------------------------------------------------------------- #
class StopKind(str, Enum):
    DEPOT = "depot"
    FARMER = "farmer"
    BUYER = "buyer"


class Stop(BaseModel):
    """One visit on a route."""

    seq: int
    kind: StopKind
    ref_id: str
    name: str
    lat: float
    lng: float
    arrival_min: int
    arrival_clock: str
    departure_min: int
    service_min: int = 0
    wait_min: int = 0
    leg_distance_m: int = 0
    cum_distance_m: int = 0
    load_kg_after: int = 0
    village: Optional[str] = None
    crop: Optional[str] = None
    quantity_kg: int = 0
    ready_from: Optional[int] = None
    spoilage_deadline_min: Optional[int] = None
    minutes_past_deadline: int = 0
    on_time: bool = True
    #: False when a greedy route dumps a crop on a buyer that does not want it.
    crop_accepted: bool = True


class RouteResult(BaseModel):
    """One truck's tour: depot -> farmers -> buyer -> depot."""

    truck_id: str
    truck_capacity_kg: int
    buyer_id: Optional[str] = None
    buyer_name: Optional[str] = None
    stops: List[Stop] = Field(default_factory=list)
    farmer_ids: List[str] = Field(default_factory=list)
    load_kg: int = 0
    kg_rejected: int = 0
    utilisation_pct: float = 0.0
    distance_m: int = 0
    distance_km: float = 0.0
    start_min: int = 0
    delivery_min: int = 0
    end_min: int = 0
    duration_min: int = 0
    cost: float = 0.0
    delivery_on_time: bool = True
    deadline_misses: int = 0


class UnservedFarmer(BaseModel):
    """A farmer no truck picked up, with the reason stated plainly."""

    farmer_id: str
    name: str
    village: str
    crop: str
    quantity_kg: int
    reason: str


class PlanResult(BaseModel):
    """The common result shape returned by both planners."""

    mode: str
    routes: List[RouteResult] = Field(default_factory=list)
    unserved: List[UnservedFarmer] = Field(default_factory=list)
    total_distance_m: int = 0
    total_distance_km: float = 0.0
    total_duration_min: int = 0
    total_cost: float = 0.0
    farmers_served: int = 0
    kg_moved: int = 0
    trucks_used: int = 0
    solver_status: str = "UNKNOWN"
    solve_seconds: float = 0.0
    notes: List[str] = Field(default_factory=list)

    def truck_of_farmer(self, farmer_id: str) -> Optional[str]:
        for route in self.routes:
            if farmer_id in route.farmer_ids:
                return route.truck_id
        return None

    def served_farmer_ids(self) -> List[str]:
        return [fid for route in self.routes for fid in route.farmer_ids]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
class PlanMetrics(BaseModel):
    mode: str
    total_km: float
    total_cost: float
    farmers_served: int
    farmers_unserved: int
    kg_moved: int
    trucks_used: int
    capacity_utilisation_pct: float
    spoilage_risk_score: float
    kg_spoiled: int
    kg_spoiled_pct: float
    deadlines_met_pct: float
    buyer_windows_met_pct: float
    kg_crop_mismatch: int
    km_per_tonne: float
    cost_per_kg: float
    farmer_payout_total: float
    farmer_gain_vs_middleman: float


class MetricDelta(BaseModel):
    """One row of the baseline-vs-optimised comparison table."""

    metric: str
    unit: str
    baseline: float
    optimized: float
    delta: float
    #: Signed so that positive always means "the solver did better".
    improvement_pct: Optional[float]
    direction: str  # "lower_is_better" | "higher_is_better"
    better: bool


class FarmerPayout(BaseModel):
    farmer_id: str
    name: str
    village: str
    crop: str
    kg: int
    truck_id: str
    buyer_id: Optional[str]
    buyer_name: Optional[str]
    price_per_kg: float
    gross_revenue: float
    transport_share: float
    net_payout: float
    net_price_per_kg: float
    middleman_price_per_kg: float
    middleman_revenue: float
    gain_vs_middleman: float
    gain_pct: Optional[float]
    spoiled: bool = False
    crop_accepted: bool = True
    #: ``net_payout`` with spoiled kilos written down to zero.
    net_payout_after_spoilage: float = 0.0


class PayoutSummary(BaseModel):
    mode: str
    farmers: List[FarmerPayout] = Field(default_factory=list)
    total_gross: float = 0.0
    total_transport: float = 0.0
    total_net: float = 0.0
    total_net_after_spoilage: float = 0.0
    total_middleman: float = 0.0
    total_gain: float = 0.0
    avg_gain_pct: Optional[float] = None


class ComparisonReport(BaseModel):
    baseline: PlanMetrics
    optimized: PlanMetrics
    deltas: List[MetricDelta] = Field(default_factory=list)
    headline: List[str] = Field(default_factory=list)
    optimized_payouts: PayoutSummary
    baseline_payouts: PayoutSummary


class RouteChange(BaseModel):
    farmer_id: str
    name: str
    crop: str
    quantity_kg: int
    from_truck: Optional[str] = None
    to_truck: Optional[str] = None
    change: str


class PlanDiff(BaseModel):
    """What a disruption did to the plan."""

    km_before: float
    km_after: float
    km_delta: float
    cost_before: float
    cost_after: float
    cost_delta: float
    farmers_served_before: int
    farmers_served_after: int
    farmers_served_delta: int
    kg_moved_before: int
    kg_moved_after: int
    kg_moved_delta: int
    trucks_used_before: int
    trucks_used_after: int
    newly_unserved: List[RouteChange] = Field(default_factory=list)
    newly_served: List[RouteChange] = Field(default_factory=list)
    reassigned: List[RouteChange] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# API request / response payloads
# --------------------------------------------------------------------------- #
class PlanMode(str, Enum):
    OPTIMIZED = "optimized"
    BASELINE = "baseline"
    BOTH = "both"


class DisruptionType(str, Enum):
    TRUCK_BREAKDOWN = "truck_breakdown"
    BUYER_CANCEL = "buyer_cancel"
    PICKUP_DELAY = "pickup_delay"


class FarmerCreate(BaseModel):
    """POST /api/farmers body. Only location, crop and quantity are required."""

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = None
    name: str
    village: str = "Unassigned"
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    crop: str
    quantity_kg: int = Field(gt=0)
    harvest_time: Optional[int] = Field(default=None, ge=0)
    ready_from: Optional[int] = Field(default=None, ge=0)
    ready_until: Optional[int] = Field(default=None, ge=0)


class BuyerCreate(BaseModel):
    """POST /api/buyers body. Prices default to a premium for the buyer type."""

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = None
    name: str
    type: BuyerType
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    crops_wanted: List[str] = Field(min_length=1)
    price_per_kg: Optional[Union[float, Dict[str, float]]] = None
    demand_kg: int = Field(gt=0)
    window_start: int = Field(default=180, ge=0)
    window_end: int = Field(default=660, ge=0)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PlanMode = PlanMode.BOTH
    # Mirrors solver.DEFAULT_TIME_LIMIT_SECONDS; repeated as a literal because
    # solver imports this module, so the dependency cannot run the other way.
    time_limit_seconds: float = Field(default=8.0, gt=0, le=120)


class DisruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: DisruptionType
    target_id: str
    delay_minutes: Optional[int] = Field(default=None, ge=0)
    time_limit_seconds: float = Field(default=8.0, gt=0, le=120)


class PlanResponse(BaseModel):
    mode: str
    dataset: Dict[str, object]
    optimized: Optional[PlanResult] = None
    baseline: Optional[PlanResult] = None
    comparison: Optional[ComparisonReport] = None


class DisruptResponse(BaseModel):
    disruption: str
    target_id: str
    description: str
    dataset: Dict[str, object]
    plan: PlanResult
    baseline: Optional[PlanResult] = None
    comparison: Optional[ComparisonReport] = None
    diff: PlanDiff


class DatasetResponse(BaseModel):
    summary: Dict[str, object]
    crops: Dict[str, Crop]
    farmers: List[Farmer]
    buyers: List[Buyer]
    trucks: List[Truck]
