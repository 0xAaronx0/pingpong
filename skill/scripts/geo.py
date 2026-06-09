"""Minimal dependency-free geohash: encode + neighbour expansion.

Used to turn the user's home coordinates into a coarse neighbourhood cell
(see docs/PROTOCOL.md §2) and to expand a search radius into the set of cells
to query. No exact coordinates ever leave the agent.
"""
from __future__ import annotations

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

_NEIGHBOURS = {
    "n": {"even": "p0r21436x8zb9dcf5h7kjnmqesgutwvy"},
    "s": {"even": "14365h7k9dcfesgujnmqp0r2twvyx8zb"},
    "e": {"even": "bc01fg45238967deuvhjyznpkmstqrwx"},
    "w": {"even": "238967debc01fg45kmstqrwxuvhjyznp"},
}
_BORDERS = {
    "n": {"even": "prxz"},
    "s": {"even": "028b"},
    "e": {"even": "bcfguvyz"},
    "w": {"even": "0145hjnp"},
}
for _d, _opp in (("n", "e"), ("s", "w"), ("e", "n"), ("w", "s")):
    _NEIGHBOURS[_d]["odd"] = _NEIGHBOURS[_opp]["even"]
    _BORDERS[_d]["odd"] = _BORDERS[_opp]["even"]


def encode(lat: float, lon: float, precision: int = 6) -> str:
    lat_iv, lon_iv = [-90.0, 90.0], [-180.0, 180.0]
    out, bit, ch, even = [], 0, 0, True
    bits = [16, 8, 4, 2, 1]
    while len(out) < precision:
        if even:
            mid = sum(lon_iv) / 2
            if lon > mid:
                ch |= bits[bit]; lon_iv[0] = mid
            else:
                lon_iv[1] = mid
        else:
            mid = sum(lat_iv) / 2
            if lat > mid:
                ch |= bits[bit]; lat_iv[0] = mid
            else:
                lat_iv[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_BASE32[ch]); bit, ch = 0, 0
    return "".join(out)


def adjacent(geohash: str, direction: str) -> str:
    geohash = geohash.lower()
    last, parent = geohash[-1], geohash[:-1]
    typ = "even" if (len(geohash) % 2 == 0) else "odd"
    if parent and last in _BORDERS[direction][typ]:
        parent = adjacent(parent, direction)
    return parent + _BASE32[_NEIGHBOURS[direction][typ].index(last)]


def neighbours(geohash: str) -> list[str]:
    n, s = adjacent(geohash, "n"), adjacent(geohash, "s")
    return [
        n, s, adjacent(geohash, "e"), adjacent(geohash, "w"),
        adjacent(n, "e"), adjacent(n, "w"), adjacent(s, "e"), adjacent(s, "w"),
    ]


def expand(center: str, rings: int) -> list[str]:
    """All cells within Chebyshev distance `rings` of center (inclusive)."""
    cells = {center}
    frontier = {center}
    for _ in range(max(0, rings)):
        nxt = set()
        for c in frontier:
            for nb in neighbours(c):
                if nb not in cells:
                    cells.add(nb); nxt.add(nb)
        frontier = nxt
    return sorted(cells)


if __name__ == "__main__":  # quick manual check
    import sys
    lat, lon = float(sys.argv[1]), float(sys.argv[2])
    prec = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    cell = encode(lat, lon, prec)
    print("cell:", cell)
    print("ring1:", expand(cell, 1))
