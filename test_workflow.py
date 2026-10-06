#!/usr/bin/env python3
"""End-to-end tests for the three-sided marketplace.

Runs a real uvicorn server against a throwaway SQLite database and drives it
over HTTP with the standard library, so nothing here is mocked: the auth, the
price gate inside the solver, the acceptance rules, the background re-planner
and the notifications are all the ones that ship.

Covered:

* login works, and a wrong password or a missing token does not;
* a farmer reading another farmer's lot gets 403, and so does a driver reaching
  for someone else's trip;
* a lot priced above every offer is unmatched with the *price* reason, and
  lowering its price gets it matched;
* a trip confirms only when its driver **and** every farmer on it accept, and
  the buyer is notified when it does;
* a confirmed trip survives the next re-plan unchanged;
* a farmer decline withdraws the lot, and a re-plan that changes a trip someone
  already accepted clears that acceptance and tells them;
* a driver decline takes the truck out and re-plans what it was carrying;
* the version counter advances, and two sessions of one driver agree.

Usage::

    python test_workflow.py            # ~1-2 minutes
    python test_workflow.py -v         # also print every HTTP call

Exits 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

#: Keep the suite quick: a 2 second solve is plenty for 25 farmers, and the
#: PBKDF2 cost is a parameter of the same code path, not a different one.
SOLVER_SECONDS = "2"
PBKDF2_ITERATIONS = "20000"

WIDTH = 100
BASE = ""  # filled in once the server is up


class Checks:
    def __init__(self) -> None:
        self.rows: List[Tuple[bool, str, str, str]] = []

    def check(self, label: str, ok: bool, info: str = "", detail: str = "") -> bool:
        self.rows.append((bool(ok), label, info, detail))
        return bool(ok)

    @property
    def failures(self):
        return [r for r in self.rows if not r[0]]

    def report(self) -> bool:
        print("\n" + "=" * WIDTH)
        print("CHECKS")
        print("=" * WIDTH)
        for ok, label, info, detail in self.rows:
            shown = info if ok else (detail or info)
            line = f"  [{'PASS' if ok else 'FAIL'}] {label}"
            print(line if not shown else f"{line}\n          {shown}")
        print(f"\n  {len(self.rows) - len(self.failures)}/{len(self.rows)} checks passed")
        return not self.failures


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def call(
    method: str, path: str, token: Optional[str] = None, body: Optional[Dict[str, Any]] = None
) -> Tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("accept", "application/json")
    if data is not None:
        request.add_header("content-type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read() or b"null")
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read() or b"null")
        status = exc.code
    if VERBOSE:
        print(f"    {method} {path} -> {status}")
    return status, payload


def login(username: str, password: str = "demo123") -> str:
    status, payload = call("POST", "/api/auth/login",
                           body={"username": username, "password": password})
    if status != 200:
        raise SystemExit(f"could not log in as {username}: {status} {payload}")
    return payload["token"]


def settle(token: str, timeout: float = 90.0) -> Dict[str, Any]:
    """Wait until no re-plan is running, then return the fresh envelope."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = call("GET", "/api/me/state", token)
        if status == 200 and not payload["planning"]:
            return payload
        time.sleep(0.25)
    raise SystemExit("the planner never settled")


def trips(admin: str) -> Dict[str, Dict[str, Any]]:
    return {row["truck_id"]: row for row in settle(admin)["data"]["trips"]}


def route_signature(admin_token: str, truck_id: str) -> Tuple:
    """Everything about a trip that a re-plan must not silently change."""
    status, payload = call("GET", f"/api/trips/{truck_id}", admin_token)
    if status != 200 or not payload.get("trip"):
        return ("missing",)
    trip = payload["trip"]
    return (
        trip["buyer_id"],
        trip["load_kg"],
        trip["distance_km"],
        trip["delivery_min"],
        trip["status"],
        tuple((s["ref_id"], s["arrival_min"], s["quantity_kg"]) for s in trip["stops"]),
    )


def lot_card(token: str, lot_id: str) -> Dict[str, Any]:
    _, payload = call("GET", "/api/me/state", token)
    for card in payload["data"]["lots"]:
        if card["lot_id"] == lot_id:
            return card
    raise SystemExit(f"lot {lot_id} not in this farmer's view")


def inbox(token: str) -> List[Dict[str, Any]]:
    _, payload = call("GET", "/api/me/state", token)
    return payload.get("notifications", [])


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server(db_path: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env["KISANROUTE_TIME_LIMIT"] = SOLVER_SECONDS
    env["KISANROUTE_PBKDF2_ITERATIONS"] = PBKDF2_ITERATIONS
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(240):
        if process.poll() is not None:
            raise SystemExit("server died on startup:\n" + (process.stdout.read() if process.stdout else ""))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return process
        except Exception:
            time.sleep(0.25)
    process.kill()
    raise SystemExit("server never became healthy")


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #
def test_auth(checks: Checks) -> Dict[str, str]:
    print("\n" + "=" * WIDTH + "\n1. AUTHENTICATION\n" + "=" * WIDTH)

    status, _ = call("GET", "/health")
    checks.check("/health needs no token", status == 200, f"HTTP {status}")

    status, payload = call("POST", "/api/auth/login",
                           body={"username": "admin", "password": "demo123"})
    checks.check("login with the right password returns a token",
                 status == 200 and bool(payload.get("token")),
                 f"HTTP {status}, role {payload.get('user', {}).get('role')}")

    status, _ = call("POST", "/api/auth/login",
                     body={"username": "admin", "password": "not-the-password"})
    checks.check("a wrong password is rejected", status == 401, f"HTTP {status}")

    status, _ = call("POST", "/api/auth/login",
                     body={"username": "nobody-here", "password": "demo123"})
    checks.check("an unknown username is rejected", status == 401, f"HTTP {status}")

    status, _ = call("GET", "/api/me/state")
    checks.check("an /api route without a token is rejected", status == 401, f"HTTP {status}")

    status, _ = call("GET", "/api/me/state", "not-a-real-token")
    checks.check("an invalid token is rejected", status == 401, f"HTTP {status}")

    tokens = {name: login(name) for name in ("admin", "kiran", "f02", "f07", "t1", "t2", "b1")}
    checks.check("every seeded role can log in, including Kiran", len(tokens) == 7,
                 "admin, kiran, f02, f07, t1, t2, b1")
    _, payload = call("GET", "/api/me/state", tokens["kiran"])
    checks.check("Kiran starts with nothing posted", payload["data"]["lots"] == [],
                 f"{len(payload['data']['lots'])} lots")

    status, payload = call("POST", "/api/auth/logout", tokens["b1"])
    revoked, _ = call("GET", "/api/me/state", tokens["b1"])
    checks.check("logging out revokes the token", status == 200 and revoked == 401,
                 f"logout HTTP {status}, reuse HTTP {revoked}")
    tokens["b1"] = login("b1")
    return tokens


def test_access(checks: Checks, tokens: Dict[str, str]) -> None:
    print("\n" + "=" * WIDTH + "\n2. ACCESS CONTROL (enforced on the server)\n" + "=" * WIDTH)
    f02, f07, admin, t1 = tokens["f02"], tokens["f07"], tokens["admin"], tokens["t1"]

    status, payload = call("GET", "/api/farmers/F02", f02)
    checks.check("a farmer can read their own lot", status == 200,
                 f"HTTP {status}, status '{(payload.get('lot') or {}).get('status')}'")

    status, payload = call("GET", "/api/farmers/F07", f02)
    checks.check("a farmer reading ANOTHER farmer's lot gets 403", status == 403,
                 f"HTTP {status}: {payload.get('detail')}")

    status, _ = call("GET", "/api/farmers/F02", f07)
    checks.check("the block works in the other direction too", status == 403, f"HTTP {status}")

    status, payload = call("GET", "/api/me/state", f02)
    lots = payload["data"]["lots"]
    owned = {lot["lot_id"] for lot in lots}
    checks.check("a farmer's own state contains only their lots",
                 owned == {"F02"}, f"sees {sorted(owned)}")

    leaked = json.dumps(payload["data"])
    checks.check("no other farmer's name appears in a farmer's payload",
                 "Prakash" not in leaked and "Meshram" not in leaked,
                 "other growers on the same truck are anonymised to 'Pickup n'")

    status, _ = call("GET", "/api/dataset", f02)
    checks.check("a farmer cannot read the coordinator dataset", status == 403, f"HTTP {status}")

    status, _ = call("POST", "/api/plan", f02, {"mode": "both", "time_limit_seconds": 1})
    checks.check("a farmer cannot run the planner", status == 403, f"HTTP {status}")

    status, _ = call("POST", "/api/trips/T2/accept", t1)
    checks.check("a driver cannot accept another driver's trip", status == 403, f"HTTP {status}")

    status, _ = call("GET", "/api/trips/T2", t1)
    checks.check("a driver cannot read another driver's trip", status == 403, f"HTTP {status}")

    status, _ = call("GET", "/api/dataset", admin)
    checks.check("the coordinator can read everything", status == 200, f"HTTP {status}")


def test_price_gate(checks: Checks, tokens: Dict[str, str]) -> str:
    print("\n" + "=" * WIDTH + "\n3. THE PRICE GATE\n" + "=" * WIDTH)
    admin, kiran = tokens["admin"], tokens["kiran"]

    board = settle(admin)["data"]
    holdout = next((r for r in board["unmatched"] if r["lot_id"] == "F13"), None)
    checks.check(
        "the seeded holdout lot is unmatched with the price reason, not capacity",
        holdout is not None and holdout["reason_code"] == "price",
        f"F13: {holdout['reason'] if holdout else 'not unmatched'}",
    )
    checks.check(
        "the price message names the best offer and the spoilage time",
        holdout is not None
        and holdout["reason"].startswith("No buyer at your price. Best offer for potato today: \u20b9")
        and "Your potato spoils at" in holdout["reason"],
        holdout["reason"] if holdout else "",
    )

    status, payload = call("POST", "/api/me/lots", kiran,
                           {"crop": "tomato", "quantity_kg": 150,
                            "min_price_per_kg": 40, "harvest_time": 60})
    lot_id = (payload.get("lot") or {}).get("id")
    checks.check("Kiran can post 150 kg of tomato", status == 201 and bool(lot_id),
                 f"HTTP {status}, lot {lot_id}, minimum \u20b940/kg")
    settle(admin)
    card = lot_card(kiran, lot_id)
    checks.check(
        "a lot priced above every offer is unmatched with the price reason",
        card["status"] == "unmatched" and card["reason_code"] == "price" and not card["truck_id"],
        f"status '{card['status']}', reason '{card['reason_code']}', "
        f"best offer \u20b9{card['best_offer_per_kg']}/kg",
    )
    checks.check(
        "the farmer is told, in their bell, that nobody will pay that price",
        any(n["kind"] == "price" for n in inbox(kiran)),
        "; ".join(n["body"][:70] for n in inbox(kiran) if n["kind"] == "price") or "nothing",
    )

    status, _ = call("POST", f"/api/me/lots/{lot_id}/price", kiran, {"min_price_per_kg": 20})
    settle(admin)
    card = lot_card(kiran, lot_id)
    checks.check(
        "lowering the price to \u20b920 gets the lot matched",
        status == 200 and card["status"] == "matched" and bool(card["truck_id"]),
        f"HTTP {status}: truck {card['truck_id']}, buyer {card['buyer_name']} at "
        f"\u20b9{card['price_per_kg']}/kg, ETA {card['eta_clock']}, "
        f"net \u20b9{card['net_payout']:,.0f} vs middleman \u20b9{card['middleman_revenue']:,.0f}",
    )
    checks.check(
        "the match never pays less than the farmer asked",
        card["price_per_kg"] is not None and card["price_per_kg"] >= 20,
        f"\u20b9{card['price_per_kg']}/kg against a \u20b920 minimum",
    )
    checks.check(
        "a live farmer is not pre-accepted: they must answer the offer",
        card["my_acceptance"] == "pending" and card["can_accept"],
        f"acceptance '{card['my_acceptance']}'",
    )
    return lot_id


def test_confirmation(checks: Checks, tokens: Dict[str, str], lot_id: str) -> Optional[str]:
    print("\n" + "=" * WIDTH + "\n4. DRIVER + FARMER ACCEPT -> CONFIRMED -> BUYER NOTIFIED\n" + "=" * WIDTH)
    admin, kiran = tokens["admin"], tokens["kiran"]
    card = lot_card(kiran, lot_id)
    truck = card["truck_id"]
    if not truck:
        checks.check("Kiran's lot has a truck to confirm", False)
        return None
    driver = login(truck.lower())

    _, payload = call("GET", "/api/me/state", driver)
    stops = payload["data"]["trip"]["stops"]
    badge = next((s["badge"] for s in stops if s.get("ref_id") == lot_id), None)
    checks.check("the driver sees Kiran's stop badged 'awaiting farmer'",
                 badge == "awaiting farmer", f"badge '{badge}'")

    status, _ = call("POST", f"/api/trips/{truck}/accept", driver)
    row = trips(admin)[truck]
    checks.check(
        "the driver accepting alone does not confirm the trip",
        status == 200 and row["status"] == "proposed" and row["driver_accepted"],
        f"HTTP {status}; trip '{row['status']}', waiting on {row['awaiting_farmers']} farmer(s)",
    )

    status, _ = call("POST", f"/api/me/lots/{lot_id}/accept", kiran)
    row = trips(admin)[truck]
    checks.check(
        "the last farmer accepting confirms the trip",
        status == 200 and row["status"] == "confirmed",
        f"HTTP {status}; {truck} '{row['status']}'",
    )
    checks.check("Kiran's card now says confirmed",
                 lot_card(kiran, lot_id)["status"] == "confirmed",
                 f"status '{lot_card(kiran, lot_id)['status']}'")

    buyer_token = login(row["buyer_id"].lower())
    note = next((n for n in inbox(buyer_token) if n["kind"] == "confirmed"), None)
    checks.check(
        "the buyer is notified with truck, ETA, crops and kilos",
        note is not None and note["body"].startswith(f"Order confirmed \u2014 {truck} on the way, ETA ")
        and " kg" in note["body"],
        note["body"] if note else "no confirmation notification",
    )
    _, payload = call("GET", "/api/me/state", buyer_token)
    confirmed = [o["truck_id"] for o in payload["data"]["confirmed"]]
    checks.check("the buyer's orders list it under Confirmed", truck in confirmed,
                 f"confirmed orders: {confirmed}")
    return truck


def test_confirmed_survives_replan(checks: Checks, tokens: Dict[str, str], truck: Optional[str]) -> None:
    print("\n" + "=" * WIDTH + "\n5. A CONFIRMED TRIP SURVIVES THE NEXT RE-PLAN\n" + "=" * WIDTH)
    if not truck:
        checks.check("a confirmed trip exists to test", False)
        return
    admin = tokens["admin"]
    before = route_signature(admin, truck)
    version_before = settle(admin)["version"]
    call("POST", "/api/me/lots", tokens["f07"],
         {"crop": "onion", "quantity_kg": 140, "min_price_per_kg": 10, "harvest_time": 120})
    after_env = settle(admin)
    after = route_signature(admin, truck)
    checks.check("posting a lot triggered a re-plan", after_env["version"] > version_before,
                 f"version {version_before} -> {after_env['version']}")
    checks.check(
        f"{truck}'s confirmed route is byte-identical afterwards",
        before == after,
        f"{len(before[5])} stops, {before[1]} kg, {before[2]} km, status '{after[4]}'",
        detail=f"before={before}\n          after ={after}",
    )


def test_farmer_decline_and_change_notice(checks: Checks, tokens: Dict[str, str],
                                          locked: Optional[str]) -> None:
    print("\n" + "=" * WIDTH + "\n6. FARMER DECLINE + 'YOUR TRIP CHANGED'\n" + "=" * WIDTH)
    admin, f02 = tokens["admin"], tokens["f02"]
    status, payload = call("POST", "/api/me/lots", f02,
                           {"crop": "tomato", "quantity_kg": 120,
                            "min_price_per_kg": 15, "harvest_time": 45})
    lot_id = (payload.get("lot") or {}).get("id")
    settle(admin)
    card = lot_card(f02, lot_id)
    truck = card["truck_id"]
    checks.check("a second live lot is matched and waiting for its farmer",
                 card["status"] == "matched" and bool(truck) and truck != locked,
                 f"{lot_id} on {truck}, status '{card['status']}'")
    if not truck:
        return

    driver = login(truck.lower())
    call("POST", f"/api/trips/{truck}/accept", driver)
    row = trips(admin)[truck]
    checks.check("its driver accepts, so the trip waits only on that farmer",
                 row["driver_accepted"] and row["status"] == "proposed",
                 f"{truck} '{row['status']}', driver in, {row['awaiting_farmers']} farmer pending")

    status, _ = call("POST", f"/api/me/lots/{lot_id}/decline", f02)
    settle(admin)
    card = lot_card(f02, lot_id)
    checks.check("a farmer decline withdraws the lot", status == 200 and card["status"] == "withdrawn"
                 and not card["truck_id"], f"HTTP {status}, status '{card['status']}'")

    row = trips(admin)[truck]
    checks.check("the re-plan changed that trip, so the driver's acceptance was cleared",
                 not row["driver_accepted"],
                 f"{truck} '{row['status']}', driver_accepted={row['driver_accepted']}")
    changed = [n for n in inbox(driver) if n["kind"] == "changed"]
    checks.check("and the driver was told: 'Your trip changed \u2014 please review.'",
                 any("Your trip changed \u2014 please review." in n["body"] for n in changed),
                 changed[0]["body"] if changed else "no change notice")


def test_driver_decline(checks: Checks, tokens: Dict[str, str], locked: Optional[str]) -> None:
    print("\n" + "=" * WIDTH + "\n7. A DRIVER DECLINE RE-PLANS THAT TRUCK'S LOTS\n" + "=" * WIDTH)
    admin = tokens["admin"]
    board = trips(admin)
    target = next((tid for tid, row in sorted(board.items())
                   if row["status"] == "proposed" and row["pickups"] > 0 and tid != locked), None)
    if target is None:
        checks.check("a proposed trip was available to decline", False,
                     detail=f"board: { {k: v['status'] for k, v in board.items()} }")
        return
    driver = login(target.lower())
    _, payload = call("GET", f"/api/trips/{target}", driver)
    carried = [s["ref_id"] for s in payload["trip"]["stops"] if s["kind"] == "farmer"]
    version_before = settle(admin)["version"]

    status, _ = call("POST", f"/api/trips/{target}/decline", driver)
    after = settle(admin)
    rows = {row["truck_id"]: row for row in after["data"]["trips"]}
    checks.check(f"the driver of {target} can decline", status == 200,
                 f"HTTP {status}, was carrying {carried}")
    checks.check("declining advances the version counter", after["version"] > version_before,
                 f"{version_before} -> {after['version']}")
    checks.check(f"{target} is out of the round",
                 rows[target]["status"] == "declined" and rows[target]["pickups"] == 0,
                 f"status '{rows[target]['status']}'")

    moved: Dict[str, List[str]] = {}
    unexplained: List[str] = []
    dropped = 0
    for farmer_id in carried:
        _, payload = call("GET", "/api/farmers/" + farmer_id, admin)
        lot = payload["lot"]
        if lot.get("truck_id"):
            moved.setdefault(lot["truck_id"], []).append(farmer_id)
        else:
            dropped += 1
            if not lot.get("reason"):
                unexplained.append(farmer_id)
    checks.check("its lots were re-planned onto other trucks",
                 bool(moved) and all(t != target for t in moved),
                 f"moved {moved}; {dropped} left unmatched with a reason")
    checks.check("every displaced lot either moved or has a stated reason", not unexplained,
                 detail=f"no reason for {unexplained}")
    if locked:
        checks.check(f"the confirmed trip {locked} is still confirmed",
                     rows[locked]["status"] == "confirmed", f"'{rows[locked]['status']}'")


def test_versioning(checks: Checks, tokens: Dict[str, str]) -> None:
    print("\n" + "=" * WIDTH + "\n6. THE VERSION COUNTER\n" + "=" * WIDTH)
    admin = tokens["admin"]

    first = settle(admin)
    status, payload = call("GET", f"/api/me/state?since={first['version']}", admin)
    checks.check("polling with the current version reports no change",
                 status == 200 and payload["changed"] is False and payload["data"] == {},
                 f"changed={payload['changed']}, payload bytes={len(json.dumps(payload['data']))}")

    status, payload = call("GET", f"/api/me/state?since={max(0, first['version'] - 1)}", admin)
    checks.check("polling with a stale version returns the full payload",
                 payload["changed"] is True and bool(payload["data"]),
                 f"changed={payload['changed']}, {len(payload['data'].get('trips', []))} trips")

    versions = [first["version"]]
    for crop, kg in (("onion", 120), ("potato", 150)):
        call("POST", "/api/me/lots", tokens["f07"],
             {"crop": crop, "quantity_kg": kg, "min_price_per_kg": 5})
        versions.append(settle(admin)["version"])
    checks.check("the counter advances monotonically across changes",
                 all(b > a for a, b in zip(versions, versions[1:])),
                 " -> ".join(str(v) for v in versions))

    status, payload = call("GET", "/health")
    checks.check("/health reports the same version", payload.get("version") == versions[-1],
                 f"health says v{payload.get('version')}, state says v{versions[-1]}")


def test_two_sessions_same_driver(checks: Checks) -> None:
    print("\n" + "=" * WIDTH + "\n7. TWO SESSIONS, ONE DRIVER\n" + "=" * WIDTH)
    first, second = login("t4"), login("t4")
    checks.check("the same account can hold two independent sessions", first != second,
                 "two distinct bearer tokens")

    _, one = call("GET", "/api/me/state", first)
    _, two = call("GET", "/api/me/state", second)
    checks.check("both sessions see the identical trip",
                 json.dumps(one["data"], sort_keys=True) == json.dumps(two["data"], sort_keys=True),
                 f"version {one['version']} in both, "
                 f"trip {((one['data'] or {}).get('trip') or {}).get('truck_id')}")


# --------------------------------------------------------------------------- #
def main() -> int:
    global BASE
    checks = Checks()
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="kisanroute-test-") as tmp:
        db_path = os.path.join(tmp, "test.db")
        print(f"starting server on 127.0.0.1:{port} with a throwaway database")
        server = start_server(db_path, port)
        BASE = f"http://127.0.0.1:{port}"
        try:
            tokens = test_auth(checks)
            settle(tokens["admin"])          # let the first plan land
            test_access(checks, tokens)
            lot_id = test_price_gate(checks, tokens)
            locked = test_confirmation(checks, tokens, lot_id)
            test_confirmed_survives_replan(checks, tokens, locked)
            test_farmer_decline_and_change_notice(checks, tokens, locked)
            test_driver_decline(checks, tokens, locked)
            test_versioning(checks, tokens)
            test_two_sessions_same_driver(checks)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    ok = checks.report()
    print("\n" + ("WORKFLOW TESTS PASSED" if ok else "WORKFLOW TESTS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
