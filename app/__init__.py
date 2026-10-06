"""KisanRoute — smallholder produce aggregation and pickup routing.

A single FastAPI service where farmers list lots, the planner proposes one
truck trip per vehicle, drivers accept or decline them, and every signed-in
browser sees the same live plan.

Modules
-------
models    : pydantic domain + result + account + request/response schemas
seed      : deterministic synthetic dataset around Nagpur, plus demo accounts
geo       : haversine distance, road factor, distance/time matrices
solver    : OR-Tools CP routing model (the optimiser)
baseline  : naive nearest-neighbour greedy planner (the control)
metrics   : metric roll-ups, solver-vs-baseline deltas, farmer payouts
db        : SQLAlchemy models — users, sessions, app_state (SQLite/PostgreSQL)
auth      : pbkdf2_hmac password hashing and bearer-token sessions
workflow  : the shared live world — trips, locking, re-planning, scoped views
main      : HTTP API
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
