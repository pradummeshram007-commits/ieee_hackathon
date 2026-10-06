"""Deterministic synthetic dataset: 25 farmers around Nagpur, 3 buyers, 4 trucks.

Everything is seeded with ``random.seed(42)`` so the demo is byte-identical on
every run and on every machine.

The dataset is *designed*, not just random, because the whole point of the demo
is that the trade-offs are real and visible:

* Farmers sit in five village clusters 12-56 km from the city, so "group nearby
  farmers" produces visibly meaningful clusters rather than a hairball.
* Total supply is ~113% of total truck capacity, so the planner **must** choose
  what to leave behind. A planner that serves everybody is not solving anything.
* Highly perishable lots (leafy greens, 5h; tomato, 6h) are deliberately
  planted both next to the city *and* in the furthest village. The far
  perishables are the trap: a nearest-neighbour greedy route reaches them hours
  after they have spoiled, while the optimiser either sends a truck out early
  or consciously drops them.
* Long-shelf crops (potato 72h, onion 48h, orange 36h) dominate the mid and far
  villages, which keeps the instance feasible instead of hopeless.
"""

from __future__ import annotations

import random
from typing import Dict, List, Tuple

from .geo import Point, offset_point, road_distance_m
from .models import Buyer, BuyerType, Crop, Dataset, Farmer, Truck

DEFAULT_SEED = 42
random.seed(DEFAULT_SEED)

#: Nagpur city centre, also the fleet depot.
NAGPUR = Point(21.1458, 79.0882, "Nagpur depot")

#: Crop catalogue: spoilage window and farm-gate reference price (INR/kg).
CROP_CATALOGUE: Dict[str, Crop] = {
    "tomato": Crop(name="tomato", spoilage_hours=6, base_price_per_kg=18.0),
    "leafy_greens": Crop(name="leafy_greens", spoilage_hours=5, base_price_per_kg=22.0),
    "onion": Crop(name="onion", spoilage_hours=48, base_price_per_kg=15.0),
    "orange": Crop(name="orange", spoilage_hours=36, base_price_per_kg=32.0),
    "banana": Crop(name="banana", spoilage_hours=24, base_price_per_kg=20.0),
    "potato": Crop(name="potato", spoilage_hours=72, base_price_per_kg=12.0),
}

#: Village centre as (name, bearing from Nagpur in degrees, distance in km,
#: crop grown by each of its five farmers).
VILLAGES: List[Tuple[str, float, float, List[str]]] = [
    ("Hingna", 232.0, 12.0, ["leafy_greens", "leafy_greens", "tomato", "tomato", "tomato"]),
    ("Kalmeshwar", 318.0, 25.0, ["tomato", "tomato", "leafy_greens", "onion", "onion"]),
    ("Saoner", 352.0, 38.0, ["onion", "onion", "potato", "tomato", "banana"]),
    ("Umred", 128.0, 52.0, ["orange", "orange", "orange", "banana", "banana"]),
    ("Katol", 300.0, 56.0, ["leafy_greens", "tomato", "potato", "potato", "onion"]),
]

#: Farmers are scattered this far (km) around their village centre.
VILLAGE_SPREAD_KM = (0.4, 2.6)

FARMER_NAMES: List[str] = [
    "Anil Deshmukh", "Sunita Pawar", "Ramesh Kale", "Vandana Thakre", "Gopal Bhoyar",
    "Kavita Raut", "Prakash Meshram", "Shalini Gawande", "Dinesh Wankhede", "Asha Chavan",
    "Manoj Dhote", "Rekha Ingle", "Sanjay Tembhurne", "Nanda Umate", "Vilas Kedar",
    "Jyoti Bawane", "Arun Gedam", "Mangala Shende", "Subhash Nagrale", "Lata Hiwrale",
    "Kishore Mohod", "Usha Bansod", "Pandurang Lokhande", "Shobha Khobragade",
    "Devidas Chaudhari",
]

#: When each crop comes off the field (minutes from plan start = 06:00).
#: Leafy greens and tomato are cut at dawn, which is exactly why their 5-6 hour
#: spoilage clock is the binding constraint of the whole model.
HARVEST_WINDOWS: Dict[str, Tuple[int, int]] = {
    "leafy_greens": (0, 75),
    "tomato": (20, 140),
    "banana": (60, 220),
    "orange": (60, 240),
    "onion": (90, 300),
    "potato": (90, 330),
}

#: Price premium over base price, by buyer type. Beating the middleman's 0.65x
#: is the farmer-facing headline of the project.
PREMIUM_BY_TYPE: Dict[str, float] = {
    BuyerType.RETAILER.value: 1.45,
    BuyerType.MANDI.value: 1.25,
    BuyerType.PROCESSOR.value: 1.35,
}

#: Supply is deliberately this multiple of fleet capacity.
SUPPLY_TO_CAPACITY_RATIO = 1.13

#: Latest minute any farmer will still hand over a lot.
LATEST_HANDOVER_MIN = 840


def default_price_for(crop: Crop, buyer_type: BuyerType | str) -> float:
    """Premium price a buyer of this type pays for this crop (INR/kg)."""
    key = buyer_type.value if isinstance(buyer_type, BuyerType) else str(buyer_type)
    return round(crop.base_price_per_kg * PREMIUM_BY_TYPE.get(key, 1.3), 2)


def _price_table(crops_wanted: List[str], buyer_type: BuyerType) -> Dict[str, float]:
    return {
        crop: default_price_for(CROP_CATALOGUE[crop], buyer_type)
        for crop in crops_wanted
        if crop in CROP_CATALOGUE
    }


def build_trucks() -> List[Truck]:
    """Four trucks of mixed capacity, 5200 kg of fleet in total."""
    spec = [("T1", 800, 14.0), ("T2", 1200, 17.0), ("T3", 1200, 17.5), ("T4", 2000, 22.0)]
    return [
        Truck(
            id=tid,
            capacity_kg=cap,
            cost_per_km=rate,
            speed_kmph=30.0,
            depot_lat=NAGPUR.lat,
            depot_lng=NAGPUR.lng,
        )
        for tid, cap, rate in spec
    ]


def build_buyers() -> List[Buyer]:
    """Three buyers with disjoint-but-overlapping crop interest.

    Note that leafy greens have exactly **one** outlet (the retailer). That is
    not an accident: it forces the optimiser to decide whether a truck heading
    to the retailer is worth sending 56 km out to Katol, and it means a greedy
    planner that fills a truck with onions and potatoes has nowhere to put the
    greens it also picked up.
    """
    specs = [
        {
            "id": "B1",
            "name": "Shakti Retail Mart",
            "type": BuyerType.RETAILER,
            "lat": 21.1350,
            "lng": 79.0750,
            "crops_wanted": ["tomato", "leafy_greens", "banana"],
            "demand_kg": 2400,
            "window_start": 180,
            "window_end": 600,
        },
        {
            "id": "B2",
            "name": "Kalamna Mandi",
            "type": BuyerType.MANDI,
            "lat": 21.1620,
            "lng": 79.1500,
            "crops_wanted": ["tomato", "onion", "potato", "orange"],
            "demand_kg": 2600,
            "window_start": 150,
            "window_end": 660,
        },
        {
            "id": "B3",
            "name": "Vidarbha Agro Processors",
            "type": BuyerType.PROCESSOR,
            "lat": 21.0550,
            "lng": 79.0300,
            "crops_wanted": ["orange", "banana", "potato", "onion"],
            "demand_kg": 2200,
            "window_start": 240,
            "window_end": 720,
        },
    ]
    return [
        Buyer(price_per_kg=_price_table(s["crops_wanted"], s["type"]), **s)  # type: ignore[arg-type]
        for s in specs
    ]


def _rebalance_quantities(quantities: List[int], target_total: int) -> List[int]:
    """Scale quantities so they sum to exactly ``target_total``.

    Keeps the generated spread but pins the supply/capacity ratio, so the
    "supply slightly exceeds the fleet" property is a guarantee rather than a
    coincidence of the RNG.
    """
    raw_total = sum(quantities)
    if raw_total <= 0:
        return quantities
    scale = target_total / raw_total
    scaled = [max(60, int(round(q * scale))) for q in quantities]
    residual = target_total - sum(scaled)
    # Push the rounding residual onto the largest lots, one kilo at a time.
    order = sorted(range(len(scaled)), key=lambda i: (-scaled[i], i))
    cursor = 0
    while residual != 0 and order:
        i = order[cursor % len(order)]
        step = 1 if residual > 0 else -1
        if scaled[i] + step >= 60:
            scaled[i] += step
            residual -= step
        cursor += 1
        if cursor > 10 * len(order):  # pathological input; stop rather than spin
            break
    return scaled


def build_farmers(trucks: List[Truck]) -> List[Farmer]:
    """25 farmers in five village clusters, with harvest and pickup windows."""
    target_total = int(round(sum(t.capacity_kg for t in trucks) * SUPPLY_TO_CAPACITY_RATIO))

    draft: List[Dict[str, object]] = []
    quantities: List[int] = []
    index = 0

    for village_name, bearing, distance_km, crops in VILLAGES:
        centre = offset_point(NAGPUR, bearing, distance_km, village_name)
        for crop_name in crops:
            jitter_bearing = random.uniform(0.0, 360.0)
            jitter_km = random.uniform(*VILLAGE_SPREAD_KM)
            location = offset_point(centre, jitter_bearing, jitter_km, village_name)

            lo, hi = HARVEST_WINDOWS[crop_name]
            harvest_time = random.randint(lo, hi)
            ready_from = harvest_time + random.randint(15, 45)
            ready_until = min(ready_from + random.randint(300, 540), LATEST_HANDOVER_MIN)

            draft.append(
                {
                    "id": f"F{index + 1:02d}",
                    "name": FARMER_NAMES[index],
                    "village": village_name,
                    "lat": location.lat,
                    "lng": location.lng,
                    "crop": crop_name,
                    "harvest_time": harvest_time,
                    "ready_from": ready_from,
                    "ready_until": ready_until,
                }
            )
            quantities.append(random.randint(110, 430))
            index += 1

    for record, qty in zip(draft, _rebalance_quantities(quantities, target_total)):
        record["quantity_kg"] = qty

    return [Farmer(**record) for record in draft]  # type: ignore[arg-type]


def build_dataset(seed: int = DEFAULT_SEED) -> Dataset:
    """Build the full planning world. Deterministic for a given ``seed``."""
    random.seed(seed)
    trucks = build_trucks()
    return Dataset(
        crops=dict(CROP_CATALOGUE),
        farmers=build_farmers(trucks),
        buyers=build_buyers(),
        trucks=trucks,
        seed=seed,
    )


def farmer_distance_km(dataset: Dataset, farmer: Farmer) -> float:
    """Road distance from the depot to a farmer, in km (reporting helper)."""
    depot = Point(dataset.trucks[0].depot_lat, dataset.trucks[0].depot_lng) if dataset.trucks else NAGPUR
    return round(road_distance_m(depot, Point(farmer.lat, farmer.lng)) / 1000.0, 2)
