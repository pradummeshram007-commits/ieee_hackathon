# KisanRoute

Smallholder produce aggregation and pickup routing.

Farmers list perishable lots. Buyers post demand. A planner groups nearby
farmers into truck pickup routes that respect **truck capacity**, **per-crop
spoilage deadlines** and **buyer delivery windows** at the same time — and
hands each farmer a payout that beats the middleman price.

One FastAPI service, one Render web service, no database, no maps API, no
network calls at runtime.

---

## Why it is not just a routing demo

A tomato lot cut at 07:00 is worthless by 13:00. That single fact turns a
textbook capacitated VRP into something a greedy planner cannot do: the route
must be feasible in *time*, per crop, per lot, while still filling the trucks
and hitting the buyer's dock inside its window.

The service ships two planners so the difference is measurable rather than
asserted:

| | `app/solver.py` | `app/baseline.py` |
|---|---|---|
| Method | OR-Tools CP routing, guided local search | nearest-neighbour greedy |
| Capacity | hard constraint | respected |
| Spoilage deadline | **hard constraint** | **ignored** |
| Buyer time window | **hard constraint** | ignored (waits if early, late if late) |
| Crop/buyer match | hard constraint | one buyer per truck, chosen after loading |

On the seeded dataset (25 farmers, 4 trucks, 8-second solve):

```
total_km                  808.35  ->  702.55   (+13.1% better)
total_cost (INR)       15,140.54  -> 12,611.62 (+16.7% better)
kg_spoiled                   478  ->        0  (+100%)
deadlines met                91%  ->     100%
buyer windows met            50%  ->     100%
kg to the wrong buyer      1,105  ->        0
farmer net payout (INR)   95,990  ->  121,989  (+27.1%)
```

The optimiser serves 21 farmers to the baseline's 22 but moves *more* kilos
(5,028 vs 4,954): it drops small lots rather than large ones, and never drops
one it could have collected in time. Supply is deliberately 113% of fleet
capacity, so something always has to be left behind — and every unserved lot
comes back with a stated reason.

---

## Architecture

```
HTTP  ──►  app/main.py          FastAPI: dataset CRUD, /plan, /disrupt, /reset
                │               in-memory AppState + one process-wide lock
                ├──►  app/seed.py       deterministic dataset (random.seed(42))
                ├──►  app/solver.py     OR-Tools CP routing model   ─┐
                ├──►  app/baseline.py   naive greedy planner        ─┤ same
                │                                                    │ PlanResult
                └──►  app/metrics.py    metrics, deltas, payouts, diffs
                              ▲
        app/models.py  pydantic schemas (domain + results + payloads)
        app/geo.py     haversine, 1.3 road factor, integer matrices
        app/static/    single-page frontend (Leaflet map, Chart.js, no build step)
```

| File | Responsibility |
|---|---|
| `app/models.py` | Every pydantic schema: domain (`Farmer`, `Buyer`, `Truck`, `Crop`, `Dataset`), results (`Stop`, `RouteResult`, `PlanResult`), metrics (`PlanMetrics`, `MetricDelta`, `FarmerPayout`, `PlanDiff`), and the request/response bodies. Also the domain rules `spoilage_deadline()` and `pickup_window()`. |
| `app/geo.py` | Haversine in metres, `ROAD_FACTOR = 1.3`, distance/time matrix builders, `offset_point()` for placing villages at an exact bearing and distance. The only place floats become the integers OR-Tools needs. |
| `app/seed.py` | 25 farmers in 5 village clusters 12–56 km around Nagpur, 3 buyers, 4 trucks (800/1200/1200/2000 kg), 6-crop catalogue. Supply pinned to 113% of fleet capacity. |
| `app/solver.py` | The OR-Tools model. See below. |
| `app/baseline.py` | Greedy control group, returning the identical `PlanResult` shape. |
| `app/metrics.py` | Metric roll-ups, signed solver-vs-baseline deltas, the farmer payout split, and plan diffing for disruptions. |
| `app/main.py` | The HTTP surface, in-memory state, and the static mount. |
| `app/static/` | The demo frontend: `index.html`, `app.js`, `style.css`. No framework, no build step, no npm — Leaflet and Chart.js come from pinned CDN URLs with SRI hashes. Served by the `StaticFiles` mount at `/`. |
| `smoke_test.py` | Runs the whole pipeline with no server; prints the full report and verifies 35 checks. |

### The OR-Tools model

Node layout, in this order, so **node 0 is the depot**:

```
[ depots ]  [ one node per modelled farmer ]  [ one END node per vehicle ]
```

**A vehicle is a `(truck, buyer)` pair.** Truck `T1` with three buyers becomes
three OR-Tools vehicles, each starting at `T1`'s depot and *ending at its
buyer's location*, plus a constraint that at most one of those three clones may
be used. Which buyer a truck serves therefore stays a decision the solver
makes.

Modelling the buyer as the route's **end node** (rather than as a mandatory
final stop) is the cleaner of the two options, because:

1. "Visited last, exactly once" becomes structural. The alternative needs the
   buyer as a regular node *plus* precedence constraints *plus* a forced-visit
   disjunction, all of which interact badly with the farmer drop penalties.
2. The load dimension stays monotonically increasing — positive demand at
   farmer nodes, nothing negative — so capacity is just the maximum cumulative
   pickup. No pickup-and-delivery pairing.
3. The buyer's delivery window is literally the cumulative-time variable at the
   route end: one `SetRange` on `CumulVar(End(v))`.
4. Per-vehicle end nodes make each clone's geography explicit, so reading the
   solution back needs no guessing about which buyer a truck served.

The one wrinkle — a real truck still has to get home — is handled by folding
the buyer→depot leg into the **cost** of the arc entering the end node. The
objective therefore prices the true round trip, while the **time** dimension
keeps measuring arrival at the buyer, which is what the delivery window
constrains. OR-Tools charges nothing for an empty route's start→end arc, so
an unused truck stays free.

| Model element | Implementation |
|---|---|
| Objective | Per-vehicle arc cost = metres × the truck's INR/km, in **paise** so per-arc rounding never swallows a real difference. |
| Capacity | `AddDimensionWithVehicleCapacity` on `quantity_kg`, zero slack, per-truck capacities. |
| Time | `AddDimensionWithVehicleTransits` (per-truck speeds), 480 min slack, 1440 min horizon, start cumul free so a truck may leave the depot late. The 15 min farmer service time is folded into every outgoing arc, which makes each cumul variable mean *arrival time*. |
| Farmer window | `[ready_from, min(ready_until, harvest_time + spoilage_hours × 60)]` — the spoilage deadline is the binding half. |
| Buyer window | `CumulVar(End(v)).SetRange(window_start, window_end)`. |
| Buyer demand | `sum(load at End(v) for v delivering to b) <= b.demand_kg`. |
| Crop match | `VehicleVar(index).SetValues([-1] + compatible_vehicles)`. |
| One route per truck | `sum(NextVar(Start(v)) != End(v) for clones of a truck) <= 1`. |
| **Never fails** | Every farmer node gets `AddDisjunction([index], penalty)` with `penalty = (5000 + 60 × kg)` INR — large enough to dwarf any detour, so lots are dropped only when genuinely infeasible, smallest-first. |
| Search | `PATH_CHEAPEST_ARC` → `GUIDED_LOCAL_SEARCH`, 8 s limit, `log_search = False`. |

Two API notes, both verified against OR-Tools 9.15:

* `RoutingModel.SetAllowedVehiclesForIndex` is **not callable from Python** —
  its `absl::Span<int const>` typemap is unwired and raises `TypeError`. The
  model restricts `VehicleVar` domains directly instead, keeping `-1` in the
  domain so the node remains droppable.
* Farmers that are infeasible *by construction* (no buyer wants the crop, or
  the lot spoils before it is ready) are filtered out **before** the model is
  built. An empty allowed-vehicle set is silently read as "all vehicles", and
  an inverted `SetRange` makes the whole model infeasible even for a node that
  would have been dropped.

### Payout split

For each farmer on a shared truck:

```
revenue    = kg × the buyer's price for that crop
transport  = that truck's total cost, pro-rated by the farmer's kg
net payout = revenue − transport
middleman  = kg × base_price × 0.65        # what they get today
gain       = net payout − middleman
```

A lot whose crop the receiving buyer does not want (only the greedy planner
does this) is valued at the middleman price, because that is where it would
actually end up. Spoiled kilos are written down in a separate
`net_payout_after_spoilage` field so the headline formula above stays exactly
as specified.

---

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/> for the status page, or
<http://127.0.0.1:8000/docs> for interactive API docs.

Tested on Linux Mint / Python 3.12.3 with fastapi 0.142.2, uvicorn 0.54.0,
pydantic 2.13.5, ortools 9.15.6755. Python 3.10+ is required.

### Smoke test

```bash
python smoke_test.py                      # 8 s solve, as specified
KISANROUTE_TIME_LIMIT=2 python smoke_test.py   # quick run
```

It seeds the dataset, runs both planners, prints a side-by-side table of every
metric plus every truck's route with arrival times and the full payout split,
replays a truck breakdown, and verifies 35 checks — including that the
optimiser never picks up a spoiled lot, never overloads a truck, never misses a
buyer window, and beats the baseline on `total_km` and `kg_spoiled`. Exits
non-zero on any failure.

---

## The frontend

`app/static/` is a single page with no build step: open `/` and it loads the
dataset, runs both planners, and renders.

* **Headline tiles** — kg spoiled, extra rupees in farmers' hands, distance
  saved. Sized for a projector.
* **Map** — Leaflet over OpenStreetMap. Farmers are circles coloured by crop
  and sized by quantity; buyers are diamonds; unserved lots are hollow and
  dashed, and their popup carries the solver's reason string. Toggle between
  *Optimised*, *Naive baseline* and *Both side by side*; in baseline mode any
  leg that breaks a spoilage deadline or a buyer window is cased in red.
* **Comparison** — the full `MetricDelta` table plus a Chart.js bar chart of
  the four headline metrics, each scaled against the larger of its two values
  so they share one axis.
* **Payouts** — every served farmer with gross, transport share, net, the
  middleman comparison and the gain, sortable by any column, with a totals row.
* **Disrupt bar** — fixed to the bottom: break a truck, cancel a buyer, or
  delay a harvest, then watch the re-plan diff and the animated route changes.

Leaflet 1.9.4 and Chart.js 4.4.1 load from pinned CDN URLs with Subresource
Integrity hashes. If either fails to load, the page says so in a banner and
keeps working without it — the tables and numbers never depend on a CDN.
`window.kisanRoute` exposes `{ state, maps, render }` for poking at a live plan
from the browser console.

---

## API

All JSON, CORS open. `GET /` serves the frontend from `app/static`.

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/health` | — | `{"status":"ok"}` |
| `GET` | `/api/dataset` | — | farmers, buyers, trucks, crop catalogue, summary |
| `POST` | `/api/farmers` | `FarmerCreate` | the created farmer + its spoilage deadline and pickup window |
| `POST` | `/api/buyers` | `BuyerCreate` | the created buyer |
| `POST` | `/api/plan` | `{"mode":"optimized"\|"baseline"\|"both","time_limit_seconds":8}` | plans + the metric comparison |
| `POST` | `/api/disrupt` | `{"type":...,"target_id":...,"delay_minutes":...}` | new plan + diff against the previous one |
| `POST` | `/api/reset` | `{"seed":42}` (optional) | re-seeded dataset summary |

```bash
curl -s localhost:8000/health

# Plan with both planners and get the full comparison
curl -s -X POST localhost:8000/api/plan \
  -H 'content-type: application/json' \
  -d '{"mode":"both","time_limit_seconds":8}' | head -c 400

# Add a farmer (times default to "cut now, 6 hours to collect")
curl -s -X POST localhost:8000/api/farmers \
  -H 'content-type: application/json' \
  -d '{"name":"Nitin Raut","village":"Hingna","lat":21.07,"lng":79.00,
       "crop":"tomato","quantity_kg":240,"harvest_time":30}'

# Add a buyer (price_per_kg may be omitted, a number, or a per-crop map)
curl -s -X POST localhost:8000/api/buyers \
  -H 'content-type: application/json' \
  -d '{"name":"Wardha Exporters","type":"processor","lat":21.02,"lng":79.19,
       "crops_wanted":["orange","banana"],"demand_kg":1500,
       "window_start":240,"window_end":720}'

# Disrupt and re-plan
curl -s -X POST localhost:8000/api/disrupt \
  -H 'content-type: application/json' \
  -d '{"type":"truck_breakdown","target_id":"T4"}'
curl -s -X POST localhost:8000/api/disrupt \
  -H 'content-type: application/json' \
  -d '{"type":"buyer_cancel","target_id":"B1"}'
curl -s -X POST localhost:8000/api/disrupt \
  -H 'content-type: application/json' \
  -d '{"type":"pickup_delay","target_id":"F07","delay_minutes":150}'

curl -s -X POST localhost:8000/api/reset -H 'content-type: application/json' -d '{}'
```

### Disruptions

| `type` | `target_id` | Effect |
|---|---|---|
| `truck_breakdown` | truck id | removes the truck from the fleet and re-plans with what is left |
| `buyer_cancel` | buyer id | removes the buyer; any crop that loses its last outlet is reported by name |
| `pickup_delay` | farmer id | slides that lot's whole timeline by `delay_minutes` — cut later, ready later, spoilage clock starts later — while buyer windows stay put, which is what makes it bite |

Each call returns the new plan plus a `diff`: kilometre/cost/kg deltas, the
farmers who lost their pickup, the ones who gained one, and every reassignment
between trucks. The "before" side of the diff is the last cached optimised plan,
or a freshly computed one if there is none, so `/api/disrupt` works as the first
call after a reset.

---

## Deploy to Render

One web service, no database, no background worker.

| Setting | Value |
|---|---|
| Environment | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/health` |
| Instance Type | Free works; Starter is steadier for an 8 s solve |
| Environment Variable | `PYTHON_VERSION = 3.12.3` |

Notes:

* **Bind to `$PORT`.** Render injects it; a service listening on a fixed port
  is marked unhealthy and redeployed in a loop.
* **No `--reload` in production.** It doubles the process and watches files for
  nothing.
* **One worker.** All state is in process memory, so a second worker would
  serve a different dataset to every other request. Scale by running one
  service, not by adding workers.
* **Free-tier CPU is shared**, so an 8-second guided local search explores less
  than it does locally. The plan is still valid — `PATH_CHEAPEST_ARC` produces
  a feasible solution within milliseconds and everything after that is
  improvement. Pass a smaller `time_limit_seconds` if a request needs to
  return faster.
* ortools ships manylinux wheels, so the build needs no compiler and takes
  about a minute.

---

## Determinism and tuning

`random.seed(42)` is set at import in `app/seed.py` and again at the top of
`build_dataset()`, so the dataset is byte-identical on every run and every
machine — the smoke test asserts it. The CP solver's RNG is pinned too where
the Python binding exposes `Solver.ReSeed`.

Guided local search runs against a **wall-clock** limit, so a slower machine
explores less of the neighbourhood and may return a slightly different (never
an invalid) plan. If you need bit-identical plans for a recorded demo, pin
`time_limit_seconds` and run on one machine; the hard constraints hold
regardless.

Constants worth knowing, all in `app/solver.py`:

| Constant | Default | Meaning |
|---|---|---|
| `SERVICE_TIME_MIN` | 15 | loading time at a farmer's gate |
| `DEFAULT_TIME_LIMIT_SECONDS` | 8 | search budget |
| `HORIZON_MIN` | 1440 | planning horizon; every window must fit inside it |
| `MAX_SLACK_MIN` | 480 | longest a truck may idle at one node |
| `DROP_PENALTY_BASE_INR` | 5000 | flat cost of abandoning a lot |
| `DROP_PENALTY_PER_KG_INR` | 60 | per-kg cost, so small lots are dropped first |

---

## Known trade-offs

* **The buyer is the end of the route, not a stop on it.** A truck serves
  exactly one buyer per trip. Multi-buyer trips would need the buyer modelled
  as a regular node with precedence constraints — a worthwhile extension, and
  the reason the formulation choice is documented rather than assumed.
* **Distances are haversine × 1.3**, not road distances. No maps API was
  allowed, and the factor is the standard circuity estimate for Indian district
  roads. Swapping in a real matrix means replacing one function in `app/geo.py`.
* **Unloading at the buyer is instantaneous.** Only the 15 min farmer service
  time is modelled, as specified.
* **State is in process memory.** A restart re-seeds. That is a deliberate
  choice for a single-service deployment, not an oversight.
