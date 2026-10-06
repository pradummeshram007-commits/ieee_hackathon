"""KisanRoute — smallholder produce aggregation and pickup routing.

A single FastAPI service that turns scattered smallholder harvests into
capacity- and spoilage-feasible truck pickup routes using OR-Tools.

Modules
-------
models    : pydantic domain + result + request/response schemas
seed      : deterministic synthetic dataset around Nagpur
geo       : haversine distance, road factor, distance/time matrices
solver    : OR-Tools CP routing model (the optimiser)
baseline  : naive nearest-neighbour greedy planner (the control)
metrics   : metric roll-ups, solver-vs-baseline deltas, farmer payouts
main      : HTTP API
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
