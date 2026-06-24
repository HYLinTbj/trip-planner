"""Encoded-polyline decoding — shared by the routing-engine clients (HYL-70).

Route geometry comes back encoded: Valhalla's `trip.legs[*].shape` and OSRM's
`geometries=polyline6` both use the Google polyline algorithm at **precision 6**
(1e6). One decoder serves both so `valhalla.route_geometry` / `osrm.route_geometry`
can return plain (lat, lon) paths the frontend draws directly.
"""


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode an encoded polyline into a list of (lat, lon) pairs.

    Standard Google/Valhalla algorithm; `precision` is the number of decimal places
    the encoder used (6 for Valhalla `shape` and OSRM `polyline6`, 5 for classic
    Google/OSRM `polyline`). Returns [] for an empty string.
    """
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    factor = 10**precision
    length = len(encoded)
    while index < length:
        for is_lon in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / factor, lon / factor))
    return coords
