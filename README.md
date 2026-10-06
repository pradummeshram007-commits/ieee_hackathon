# KisanRoute

A live three-sided marketplace for smallholder produce.

Farmers post lots with a **minimum price**. Buyers post demand with an **offer
price**. The optimiser matches lots to buyers who want that crop *and* pay at
least the farmer's minimum, and plans truck routes that respect **truck
capacity**, **per-crop spoilage deadlines** and **buyer delivery windows** at
the same time. The farmer, the driver and the buyer each accept on their own
screen. A trip is confirmed only when the driver and every farmer on it have
said yes, and every screen hears about it within one 3-second poll.

One FastAPI service, one Render web service, SQLite (or PostgreSQL), no maps
API, no network calls at runtime.

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
| Farmer's minimum price | **hard constraint** (in the search space) | applied: never collects an unsellable lot |

On the seeded dataset (25 farmers, 4 trucks, 8-second solve), baseline → optimised:

```
sellable produce delivered     4,191 kg  ->   4,967 kg   (+18.5%)
distance per sellable tonne    152.7 km  ->   132.1 km   (+13.5% better)
cost per sellable kg           ₹2.98     ->   ₹2.35      (+21.1% better)
kg_spoiled                       149     ->       0
buyer windows met                75%     ->     100%
kg to the wrong buyer / below price  772 ->       0
farmer net payout             ₹90,467    ->  ₹1,07,211   (+18.5%)
raw distance                   640.0 km  ->   656.2 km   (−2.5%)
```

**Why the efficiency number is per *sellable* tonne.** The greedy plan is
infeasible: it collects produce after it has spoiled, misses buyer windows and
dumps lots on buyers who will not take them. Its raw distance therefore buys
less. Before the price rule existed, the optimiser also won on raw kilometres.
That was because the baseline wasted trips on produce nobody could sell. Once
the same price rule stops the baseline doing that, the two drive similar
distances (within 2.5% here), and what separates them is how much of the
cargo actually reaches a paying buyer in good condition. Measured across six
seeds, raw distance swings either way; distance per sellable tonne favours the
optimiser on the default seed and on four of the five others. `smoke_test.py`
asserts the per-sellable-tonne win, and keeps raw distance within 15% of the
baseline rather than asserting a raw-distance win the data no longer supports.

Supply is deliberately 113% of fleet capacity, so something is always left
behind, and every unmatched lot comes back with a reason **code**: `price`,
`capacity`, `window` or `no_buyer`.

---

## Architecture

```
HTTP  ──►  app/main.py          routes + auth dependencies + static mount
                │
                ├──►  app/auth.py       pbkdf2_hmac passwords, bearer tokens
                ├──►  app/db.py         SQLAlchemy: users, sessions, app_state
                │
                └──►  app/workflow.py   the shared live world
                        │               trips, locking, re-planning, scoped views
                        ├──►  app/seed.py       deterministic dataset + demo accounts
                        ├──►  app/solver.py     OR-Tools CP routing model   ─┐
                        ├──►  app/baseline.py   naive greedy planner        ─┤ same
                        │                                                    │ PlanResult
                        └──►  app/metrics.py    metrics, deltas, payouts, diffs
                              ▲
        app/models.py  pydantic schemas (domain + results + accounts + payloads)
        app/geo.py     haversine, 1.3 road factor, integer matrices
        app/static/    login + farmer/driver/buyer views + coordinator dashboard
```

| File | Responsibility |
|---|---|
| `app/models.py` | Every pydantic schema: domain (`Farmer`, `Buyer`, `Truck`, `Crop`, `Dataset`), results (`Stop`, `RouteResult`, `PlanResult`), metrics (`PlanMetrics`, `MetricDelta`, `FarmerPayout`, `PlanDiff`), and the request/response bodies. Also the domain rules `spoilage_deadline()` and `pickup_window()`. |
| `app/geo.py` | Haversine in metres, `ROAD_FACTOR = 1.3`, distance/time matrix builders, `offset_point()` for placing villages at an exact bearing and distance. The only place floats become the integers OR-Tools needs. |
| `app/seed.py` | 25 farmers in 5 village clusters 12–56 km around Nagpur, 3 buyers, 4 trucks (800/1200/1200/2000 kg), 6-crop catalogue. Supply pinned to 113% of fleet capacity. |
| `app/solver.py` | The OR-Tools model. See below. |
| `app/baseline.py` | Greedy control group, returning the identical `PlanResult` shape. |
| `app/metrics.py` | Metric roll-ups, signed solver-vs-baseline deltas, the farmer payout split, and plan diffing for disruptions. |
| `app/auth.py` | `hashlib.pbkdf2_hmac` password hashing, `secrets` bearer tokens, and the FastAPI header dependency. Standard library only. |
| `app/db.py` | SQLAlchemy models and engine. SQLite by default, PostgreSQL when `DATABASE_URL` is set. |
| `app/workflow.py` | The live world: trip lifecycle, the lock-aware re-planner, the background planning thread, the version counter, and every role-scoped view. **Access control lives here**, not in the browser. |
| `app/main.py` | The HTTP surface, auth dependencies, and the static mount. |
| `app/static/` | The frontend: `index.html` (shell + all views), `shell.js` (auth, routing, polling, farmer/driver/buyer views, i18n), `app.js` (the coordinator dashboard), `style.css`, `views.css`. No framework, no build step, no npm — Leaflet and Chart.js come from pinned CDN URLs with SRI hashes. |
| `test_workflow.py` | Starts a real server against a throwaway database and drives the whole workflow over HTTP. |
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

## Accounts and roles

| Role | Sees | Demo accounts |
|---|---|---|
| **farmer** | only their own lots and match | `f01` … `f25`, and `kiran` |
| **driver** | only their own trip | `t1` … `t4` (one per truck) |
| **buyer** | only their own orders | `b1`, `b2`, `b3` |
| **coordinator** | everything, plus the full dashboard | `admin` |

Every demo account uses the password `demo123`. The login screen has one-click
chips — **Kiran (new farmer)**, **Sunita (farmer)**, **T1–T4**, **Shakti Retail
(buyer)**, **Coordinator** — so a demo never stalls on typing. Kiran Patil farms
near Hingna and has **nothing posted**: the live demo is Kiran listing produce. Farmers can also self-register: name, village, phone,
password, and a farm location picked by clicking the map.

Passwords are hashed with `hashlib.pbkdf2_hmac` (SHA-256, 200 000 iterations,
a fresh 16-byte salt each) and compared with `secrets.compare_digest`. Tokens
are `secrets.token_urlsafe(32)`, stored in the `sessions` table, sent as
`Authorization: Bearer <token>` and kept in `localStorage`. Every `/api` route
except login and register needs one; `/health` stays open so a platform health
check works.

**Access is enforced on the server.** `/api/me/state` is assembled from the
caller's own username, so a farmer's payload never contains another farmer's
payout — there is nothing for the browser to filter. Endpoints that name a
resource check the caller and return 403:

```
f02 → GET /api/farmers/F02    200   (their own lot)
f02 → GET /api/farmers/F07    403   (another farmer's lot)
f02 → GET /api/dataset        403   (coordinator only)
t1  → POST /api/trips/T2/accept  403   (not their truck)
```

One deliberate nuance: a farmer's map shows their truck's whole route, because
that is what the brief asks for, but the other growers on that truck are
reduced to "Pickup 3" — no name, no quantity, no payout.

---

## Prices and matching

A lot may ride a truck only if that truck's buyer **wants the crop and offers
at least the farmer's `min_price_per_kg`**. The solver enforces both halves with
the same `VehicleVar(index).SetValues(...)` restriction it already used for
crop compatibility, so an unaffordable match is not discouraged by the
objective; it is simply not in the search space. The baseline applies the
identical rule (`_buyer_takes`), so the comparison stays fair.

A lot no buyer will pay for never reaches the routing model. It comes back with
reason code `price` and this message, rendered on the farmer's card with an
**Edit price** button:

> No buyer at your price. Best offer for tomato today: ₹26.10/kg. Your tomato
> spoils at 13:00.

Seeded minimums sit 12–30% below the *lowest* offer for their crop, except
**F13** (potato), which is priced above every offer, so the coordinator's
worklist shows the price case without anyone touching a phone.

---

## The three-sided workflow

```
farmer posts a lot (crop, kg, harvest, window, min price)  ->  re-plan
planner proposes one trip per truck
   farmer card:  "Match found — accept?"  buyer, ₹/kg, truck, ETA,
                  net payout after transport, what a middleman would pay
   driver card:  stops with ETA and kg, buyer, km, fee,
                  per-stop badge "farmer confirmed" / "awaiting farmer"
driver accepts + every farmer on the trip accepts   ->  CONFIRMED (locked)
   buyer:        "Order confirmed — T2 on the way, ETA 11:11,
                  tomato 783 kg + leafy greens 417 kg."
   farmer:       "Confirmed — Truck T2 arrives ~09:14, sold to Shakti Retail
                  Mart at ₹31.90/kg, you receive ₹8,205."
```

* **Seeded farmers are pre-accepted**; only live farmers tap Accept. A trip
  carrying only seeded lots confirms the moment its driver accepts.
* **Re-plans never touch a confirmed trip.** `_solver_input()` removes the
  confirmed trucks, their lots and the buyer demand they cover; everything else
  is planned and the locked routes are merged back unchanged.
* **Re-plans are triggered** by posting a lot, editing a price, posting demand,
  and any decline. They run on a background thread behind a lock; while one
  runs every screen shows "Re-optimising…".
* **A farmer decline withdraws the lot** (it stays on their screen, so they can
  set a new price and re-post). **A driver decline** takes that truck out for
  the round, and its lots go to other trucks or come back with a reason.
* **If a re-plan changes an unconfirmed trip someone already accepted**, every
  route has a fingerprint; when it changes, those acceptances are cleared and
  the people involved get: *"Your trip changed — please review."*

Notifications are stored per user: a bell with an unread count, and a toast
when a new one arrives. Toasts never intercept a tap, so they cannot cover the
Accept button on a phone.

---

## Live updates

A server-side counter increments on every change. Each view polls
`GET /api/me/state?since=<version>` every 3 seconds; when the version has not
moved the server replies `{"changed": false}` with an empty payload and the
browser re-renders nothing.

Two browsers signed in as the same driver show the same trip, and an action in
any account reaches every affected account within one poll. Measured inside
each page from the moment of the tap, over three runs: the driver's screen
flipped to Confirmed in 1.1–3.3 s and the buyer's "Order confirmed" toast
arrived in 1.6–2.4 s.

---

## Persistence

```bash
# default: a SQLite file next to the app
uvicorn app.main:app --reload

# PostgreSQL
DATABASE_URL=postgres://user:pass@host/db uvicorn app.main:app
```

A bare `postgres://` prefix (what several hosts still hand out) is rewritten to
`postgresql+psycopg://` so the psycopg 3 driver is used. `KISANROUTE_DB_PATH`
moves the SQLite file.

Three tables: `users`, `sessions`, and `app_state` — a single row holding the
planning world as JSON (farmers, buyers, trucks, the current plan, and every
trip and stop status) next to the version counter. The world is saved after
every change and loaded at startup; an empty database is seeded.

Keeping the world as one JSON document rather than a dozen normalised tables is
deliberate: the domain objects are already pydantic models that round-trip
losslessly, every mutation is a whole-world update under one lock, and one
process serves every session. Normalising it would buy nothing and cost the
ability to hand the solver a `Dataset` directly.

---

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/> and sign in — the one-click chips on the login
screen cover a farmer, a driver, a buyer and the coordinator. Interactive API
docs are at <http://127.0.0.1:8000/docs>.

The first start creates `kisanroute.db`, seeds the dataset and the 34 demo
accounts, and runs one plan. Delete that file to start over, or sign in as
`admin` and press **Reset dataset**.

Tested on Linux Mint / Python 3.12.3 with fastapi 0.142.2, uvicorn 0.54.0,
pydantic 2.13.5, ortools 9.15.6755, sqlalchemy 2.1.3, psycopg 3.3.6.
Python 3.10+ is required.

### Tests

```bash
python smoke_test.py                           # planners, 40 checks
KISANROUTE_TIME_LIMIT=2 python smoke_test.py   # quick run
python test_workflow.py                        # marketplace flow, 52 checks
python test_workflow.py -v                     # also log every HTTP call
```

`smoke_test.py` touches no server and no database: it seeds the dataset, runs
both planners, prints a side-by-side table of every metric plus every truck's
route with arrival times and the full payout split, replays a truck breakdown,
and verifies 40 checks — including that the optimiser never picks up a spoiled
lot, never overloads a truck, never misses a buyer window, **never sells a lot
below its farmer's minimum**, and beats the baseline on sellable produce,
distance and cost per sellable unit, spoilage and farmer payout.

`test_workflow.py` starts a real uvicorn server against a throwaway SQLite
database and drives it over HTTP with the standard library — nothing is mocked.
It covers login and rejected passwords, the 403s above, a lot priced above
every offer coming back unmatched with the `price` reason, lowering the price
getting it matched, a trip confirming only when the driver and every farmer
accept, the buyer being notified, a confirmed trip surviving the next re-plan
byte for byte, a farmer decline voiding a driver's acceptance (with the "Your
trip changed" notice), a driver decline re-planning that truck's lots, the
version counter, and two sessions of one driver agreeing.

Both exit non-zero on any failure.

---

## The frontend

`app/static/` is one page with no build step. `shell.js` owns login, routing by
role and the 3-second poll; `app.js` owns the coordinator dashboard and is
booted only once a coordinator signs in.

**Farmer** — one big card (status, truck, ETA, buyer, price per kg, what they
receive, and what a middleman would have paid), a map showing only their farm,
their truck's route and the buyer, a "Post new produce" form, and a language
toggle for **English / हिंदी / मराठी** covering every farmer-view label and all
six crop names.

**Driver** — their trip only: route map, ordered stops with the farmer's name,
a tappable phone number, crop, kg and time, Accept / Decline, a "Picked up"
button per stop and "Mark delivered". Large type, phone first.

**Buyer** — incoming trucks with ETA, crops and kg, a map of the trucks heading
to them, and a "Post demand" form.

**Coordinator** — the original dashboard, unchanged, plus a live trip board:

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
* **Live trip board** — every truck's status (Proposed / Accepted / Declined /
  In transit / Delivered) with its buyer, collected count, load, distance and
  driver fee, refreshed by the same 3-second poll.

Leaflet 1.9.4 and Chart.js 4.4.1 load from pinned CDN URLs with Subresource
Integrity hashes. If either fails to load, the page says so in a banner and
keeps working without it — the tables and numbers never depend on a CDN.
`window.kisanRoute` exposes `{ state, maps, render }` for poking at a live plan
from the browser console.

---

## API

All JSON, CORS open. `GET /` serves the frontend from `app/static`. Every
`/api` route except login and register needs `Authorization: Bearer <token>`.

| Method | Path | Who | Returns |
|---|---|---|---|
| `GET` | `/health` | anyone | `{"status","version","planning"}` |
| `POST` | `/api/auth/login` | anyone | `{token, user}` |
| `POST` | `/api/auth/register` | anyone | a new farmer account + token |
| `POST` | `/api/auth/logout` | signed in | revokes the token |
| `GET` | `/api/auth/me` | signed in | the current user |
| `GET` | `/api/auth/demo-accounts` | anyone | the login screen's chips |
| `GET` | `/api/me/state?since=N` | signed in | **the role-scoped payload every view polls** |
| `POST` | `/api/me/lots` | farmer | lists produce (with `min_price_per_kg`), re-plans |
| `POST` | `/api/me/lots/{lot}/accept` | that farmer | accepts the match |
| `POST` | `/api/me/lots/{lot}/decline` | that farmer | withdraws the lot, re-plans |
| `POST` | `/api/me/lots/{lot}/price` | that farmer | edits the minimum price, re-plans |
| `POST` | `/api/me/notifications/read` | signed in | clears the bell |
| `POST` | `/api/me/demand` | buyer | crop, offer ₹/kg, kg, window; re-plans |
| `GET` | `/api/trips/{truck}` | driver, buyer, farmer on it, coordinator | that trip |
| `POST` | `/api/trips/{truck}/accept` | that driver | driver's yes; confirms once every farmer agrees |
| `POST` | `/api/trips/{truck}/decline` | that driver | drops out, re-plans |
| `POST` | `/api/trips/{truck}/stops/{lot}/pickup` | that driver | marks a stop collected |
| `POST` | `/api/trips/{truck}/deliver` | that driver | ends the trip |
| `GET` | `/api/farmers/{lot}` | self or coordinator | one lot with its payout |
| `GET` | `/api/dataset` | coordinator | farmers, buyers, trucks, crops |
| `POST` | `/api/farmers`, `/api/buyers` | coordinator | add one directly |
| `POST` | `/api/plan` | coordinator | plan now, honouring locked trips |
| `POST` | `/api/disrupt` | coordinator | mutate, re-plan, diff |
| `POST` | `/api/reset` | coordinator | re-seed the world (accounts stay) |

```bash
curl -s localhost:8000/health

# Sign in and keep the token
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"demo123"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
AUTH="Authorization: Bearer $TOKEN"

# What this account may see, and the version to poll against
curl -s -H "$AUTH" localhost:8000/api/me/state | head -c 400

# Plan with both planners and get the full comparison
curl -s -X POST localhost:8000/api/plan -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"mode":"both","time_limit_seconds":8}' | head -c 400

# A farmer lists produce; a driver accepts the trip they are offered
curl -s -X POST localhost:8000/api/me/lots -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"crop":"tomato","quantity_kg":220,"harvest_time":60}'
curl -s -X POST localhost:8000/api/trips/T2/accept -H "$AUTH"

# Add a farmer (times default to "cut now, 6 hours to collect")
curl -s -X POST localhost:8000/api/farmers -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"name":"Nitin Raut","village":"Hingna","lat":21.07,"lng":79.00,
       "crop":"tomato","quantity_kg":240,"harvest_time":30}'

# Add a buyer (price_per_kg may be omitted, a number, or a per-crop map)
curl -s -X POST localhost:8000/api/buyers -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"name":"Wardha Exporters","type":"processor","lat":21.02,"lng":79.19,
       "crops_wanted":["orange","banana"],"demand_kg":1500,
       "window_start":240,"window_end":720}'

# Disrupt and re-plan
curl -s -X POST localhost:8000/api/disrupt -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"type":"truck_breakdown","target_id":"T4"}'
curl -s -X POST localhost:8000/api/disrupt -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"type":"buyer_cancel","target_id":"B1"}'
curl -s -X POST localhost:8000/api/disrupt -H "$AUTH" \
  -H 'content-type: application/json' \
  -d '{"type":"pickup_delay","target_id":"F07","delay_minutes":150}'

curl -s -X POST localhost:8000/api/reset -H "$AUTH" -H 'content-type: application/json' -d '{}'
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
| Root Directory | *(leave blank — the repo root holds `requirements.txt` and `app/`)* |
| Environment | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/health` |
| Instance Type | Free works; Starter is steadier for an 8 s solve |
| Environment Variable | `PYTHON_VERSION = 3.12.3` |
| Environment Variable | `DATABASE_URL` — a Render PostgreSQL URL, or leave unset for SQLite |

Notes:

* **Bind to `$PORT`.** Render injects it; a service listening on a fixed port
  is marked unhealthy and redeployed in a loop.
* **No `--reload` in production.** It doubles the process and watches files for
  nothing.
* **One worker.** The planning world is held in memory and written back to the
  database after each change; a second worker would keep its own copy and the
  two would diverge. Scale by running one service, not by adding workers.
* **Pick a database.** Without `DATABASE_URL` the service writes a SQLite file
  to the container's ephemeral disk, so a redeploy starts from a fresh seed —
  fine for a demo. Attach a Render PostgreSQL instance and set `DATABASE_URL`
  (a bare `postgres://` prefix is rewritten automatically) to keep accounts and
  trips across deploys.
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
* **One process owns the world.** Every mutation runs under a single lock and
  the whole planning world is rewritten on each change. That is right for one
  service and a few dozen trucks, and it is the first thing to replace if this
  ever needed to scale horizontally.
* **A declined truck stays out until Reset.** Declining is a decision about the
  day, not about one round, so the truck is not quietly re-offered the next
  time a farmer posts a lot.
* **The baseline is not lock-aware.** It re-plans the whole day from scratch
  every time, because it is the "what if nobody optimised anything" control.
  Making it respect accepted trips would flatter the comparison.
