import json

try:
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False


def _in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-18) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_poly(x, y, rings):
    if not rings or not _in_ring(x, y, rings[0]):
        return False
    for hole in rings[1:]:
        if _in_ring(x, y, hole):
            return False
    return True


class PolygonIndex:
    def __init__(self, geojson_path: str):
        with open(geojson_path) as f:
            gj = json.load(f)

        if HAVE_SHAPELY:
            geoms = [shape(feat["geometry"]) for feat in gj["features"]]
            self._tree = STRtree(geoms)
            self._geoms = geoms
            self._use_shapely = True
        else:
            parts = []
            for feat in gj["features"]:
                g = feat["geometry"]
                polys = (
                    g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
                )
                for poly in polys:
                    xs, ys = [], []
                    for ring in poly:
                        for x, y in ring:
                            xs.append(x)
                            ys.append(y)
                    parts.append(((min(xs), min(ys), max(xs), max(ys)), poly))
            self._parts = parts
            self._use_shapely = False

    def contains(self, lon: float, lat: float) -> bool:
        if self._use_shapely:
            pt = Point(lon, lat)
            for i in self._tree.query(pt):
                if self._geoms[i].contains(pt):
                    return True
            return False
        for (minx, miny, maxx, maxy), rings in self._parts:
            if lon < minx or lon > maxx or lat < miny or lat > maxy:
                continue
            if _point_in_poly(lon, lat, rings):
                return True
        return False
