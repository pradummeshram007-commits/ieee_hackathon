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
    phone: Optional[str] = Field(default=None, description="Shown to the driver coming for the lot")
    #: The floor the farmer will sell at. A buyer offering less than this may
    #: not be matched to the lot, however convenient the route would be.
    min_price_per_kg: float = Field(default=0.0, ge=0)
    #: Seeded lots are treated as already accepted so a live demo only needs
    #: the one real farmer to tap Accept.
    pre_accepted: bool = False
    #: A farmer who declines their match withdraws the lot; it stays visible to
    #: them (so they can re-post) but is kept out of every planning round.
    withdrawn: bool = False
    #: Username of the account that posted this lot. One account may own
    #: several lots, so this is the link back to ``users``, not the lot id.
    owner: Optional[str] = None

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

    def buyers_for_lot(self, farmer: Farmer) -> List[Buyer]:
        """Buyers that want this crop **and** will pay the farmer's minimum."""
        return [
            b for b in self.buyers
            if b.wants(farmer.crop) and b.price_for(farmer.crop) >= farmer.min_price_per_kg
        ]

    def best_offer_for_crop(self, crop: str) -> Optional[float]:
        """The highest price anyone is offering for this crop today."""
        offers = [b.price_for(crop) for b in self.buyers if b.wants(crop)]
        return max(offers) if offers else None

    def active_farmers(self) -> List[Farmer]:
        """Lots still in play: everything a farmer has not withdrawn."""
        return [f for f in self.farmers if not f.withdrawn]

    def total_supply_kg(self) -> int:
        return sum(f.quantity_kg for f in self.active_farmers())

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


class UnservedReason(str, Enum):
    """Why a lot went unmatched. The UI branches on this, not on prose."""

    PRICE = "price"            # buyers want the crop, none will pay the minimum
    NO_BUYER = "no_buyer"      # nobody is buying this crop at all
    WINDOW = "window"          # spoils before it can be collected
    CAPACITY = "capacity"      # the fleet ran out of room
    NO_TRUCK = "no_truck"      # no truck was available to plan with
    OTHER = "other"


class UnservedFarmer(BaseModel):
    """A farmer no truck picked up, with the reason stated plainly."""

    farmer_id: str
    name: str
    village: str
    crop: str
    quantity_kg: int
    reason: str
    reason_code: UnservedReason = UnservedReason.OTHER
    #: Set when ``reason_code`` is ``price``: the best price on offer today, so
    #: the farmer can see exactly how far they are from a sale.
    best_offer_per_kg: Optional[float] = None
    min_price_per_kg: Optional[float] = None


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
    #: Delivered on time, to a buyer that wants the crop at the farmer's price.
    #: The denominator the other efficiency numbers should really use.
    kg_sellable: int
    km_per_tonne: float
    km_per_sellable_tonne: float
    cost_per_kg: float
    cost_per_sellable_kg: float
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


# --------------------------------------------------------------------------- #
# Accounts, trips and the live shared workflow
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    FARMER = "farmer"
    DRIVER = "driver"
    BUYER = "buyer"
    COORDINATOR = "coordinator"


class TripStatus(str, Enum):
    """Lifecycle of one truck's trip in a planning round.

    ``proposed`` means the offer is out but at least one party has not answered.
    ``confirmed`` means the driver **and** every farmer on the trip accepted;
    only then is the trip locked and safe from the next re-plan.
    """

    IDLE = "idle"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"


#: Statuses a re-plan may not touch.
LOCKED_TRIP_STATUSES = frozenset(
    {TripStatus.CONFIRMED, TripStatus.IN_TRANSIT, TripStatus.DELIVERED}
)


class AcceptStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class StopStatus(str, Enum):
    PENDING = "pending"
    PICKED_UP = "picked_up"


class LotStatus(str, Enum):
    """What a farmer sees next to their lot."""

    WAITING = "waiting"          # posted, nothing matched to it yet
    MATCHED = "matched"          # a buyer and truck are offered -- accept?
    ACCEPTED = "accepted"        # this farmer said yes, trip not confirmed yet
    CONFIRMED = "confirmed"      # driver and every farmer agreed
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    DECLINED = "declined"        # the farmer turned the match down
    WITHDRAWN = "withdrawn"
    UNMATCHED = "unmatched"      # see the lot's reason_code


class Trip(BaseModel):
    """One truck's trip and the three-sided agreement around it.

    A trip only becomes binding when ``driver_accepted`` is true and every
    entry in ``farmer_status`` is ``accepted``. Until then a re-plan may move
    it, and if it does the acceptances are cleared and everyone is told.
    """

    truck_id: str
    status: TripStatus = TripStatus.IDLE
    #: The proposed (or, once confirmed, locked) route. ``None`` when idle or declined.
    route: Optional[RouteResult] = None
    driver_accepted: bool = False
    driver_declined: bool = False
    #: lot id -> whether that farmer has accepted this match.
    farmer_status: Dict[str, AcceptStatus] = Field(default_factory=dict)
    #: lot id -> whether the driver has collected it yet.
    stop_status: Dict[str, StopStatus] = Field(default_factory=dict)
    #: What the driver is paid: the route's transport cost.
    fee: float = 0.0
    #: Identifies the shape of the offer, so a re-plan can tell whether what
    #: someone already agreed to has changed underneath them.
    fingerprint: Optional[str] = None
    proposed_at: Optional[str] = None
    accepted_at: Optional[str] = None
    declined_at: Optional[str] = None
    confirmed_at: Optional[str] = None
    delivered_at: Optional[str] = None

    @property
    def is_locked(self) -> bool:
        return self.status in LOCKED_TRIP_STATUSES

    def farmer_ids(self) -> List[str]:
        return list(self.route.farmer_ids) if self.route else []

    def awaiting_farmers(self) -> List[str]:
        return [fid for fid, st in self.farmer_status.items() if st == AcceptStatus.PENDING]

    def all_farmers_accepted(self) -> bool:
        return bool(self.farmer_status) and all(
            st == AcceptStatus.ACCEPTED for st in self.farmer_status.values()
        )


class Notification(BaseModel):
    """One line in a user's bell menu."""

    id: str
    created_at: str
    kind: str          # match_offer | trip_offer | confirmed | changed | price | info
    title: str
    body: str
    read: bool = False


class UserPublic(BaseModel):
    """A user as the API hands it out: never includes the password hash."""

    username: str
    role: Role
    #: F07 for a farmer account, T2 for a driver, B1 for a buyer, None for the coordinator.
    ref_id: Optional[str] = None
    display_name: str
    village: Optional[str] = None
    phone: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    """Farmer self-registration. The farm location is picked on a map."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=256)
    name: str = Field(min_length=1, max_length=120)
    village: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=4, max_length=32)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class AuthResponse(BaseModel):
    token: str
    user: UserPublic


class LotCreate(BaseModel):
    """POST /api/me/lots -- a farmer listing produce for pickup."""

    model_config = ConfigDict(extra="forbid")

    crop: str
    quantity_kg: int = Field(gt=0, le=100_000)
    #: The farmer's floor. Leave it at 0 to accept any offer.
    min_price_per_kg: float = Field(default=0.0, ge=0, le=10_000)
    harvest_time: Optional[int] = Field(default=None, ge=0, le=1440)
    ready_from: Optional[int] = Field(default=None, ge=0, le=1440)
    ready_until: Optional[int] = Field(default=None, ge=0, le=1440)


class LotPriceUpdate(BaseModel):
    """POST /api/me/lots/{lot}/price -- the Edit price button."""

    model_config = ConfigDict(extra="forbid")

    min_price_per_kg: float = Field(ge=0, le=10_000)


class NotificationsRead(BaseModel):
    """POST /api/me/notifications/read -- omit ids to clear the whole bell."""

    model_config = ConfigDict(extra="forbid")

    ids: Optional[List[str]] = None


class DemandUpdate(BaseModel):
    """POST /api/me/demand -- a buyer posting what it wants today."""

    model_config = ConfigDict(extra="forbid")

    crops_wanted: Optional[List[str]] = Field(default=None, min_length=1)
    demand_kg: Optional[int] = Field(default=None, gt=0, le=1_000_000)
    window_start: Optional[int] = Field(default=None, ge=0, le=1440)
    window_end: Optional[int] = Field(default=None, ge=0, le=1440)
    price_per_kg: Optional[Union[float, Dict[str, float]]] = None


class StateEnvelope(BaseModel):
    """What every view polls.

    ``data`` is role-specific and assembled on the server, which is also where
    the access rules live -- a farmer's envelope simply never contains another
    farmer's payout.
    """

    version: int
    changed: bool
    planning: bool
    role: Role
    user: UserPublic
    server_time: str
    data: Dict[str, object] = Field(default_factory=dict)
