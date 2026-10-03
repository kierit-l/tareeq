"""Load the built corridor data and project it for spatial work."""
import json
from pathlib import Path

from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, shape
from shapely.ops import transform

DATA = Path(__file__).resolve().parent.parent / "data"
BUILD = DATA / "build"
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32636", always_xy=True).transform
NOGO_BUFFER_M = 300
BUFFER_BY_KIND = {"yellow_line": 500, "evacuation": 300}   # spec S4; polygon may also set buffer_m

MAJOR = {"trunk", "primary", "secondary", "tertiary", "unclassified",
         "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


class Corridor:
    def __init__(self):
        self.geojson = json.load(open(BUILD / "segments.geojson"))
        self.places = json.load(open(BUILD / "places.json"))
        self.stands = json.load(open(BUILD / "stands.json"))
        self.seg = {}
        self.node_ll = {}
        geoms_m = []
        for f in self.geojson["features"]:
            p = dict(f["properties"])
            coords = f["geometry"]["coordinates"]
            line_m = transform(to_utm, LineString(coords))
            c = line_m.interpolate(0.5, normalized=True)
            p["xy"] = (c.x, c.y)
            p["major"] = p["highway"] in MAJOR
            self.seg[p["id"]] = p
            self.node_ll.setdefault(p["u"], coords[0])
            self.node_ll.setdefault(p["v"], coords[-1])
            geoms_m.append(line_m)
        self._ids = [f["properties"]["id"] for f in self.geojson["features"]]
        self._tree = STRtree(geoms_m)
        self._geoms_m = geoms_m
        self.nogo = self.load_nogo()

    def load_nogo(self):
        fc = json.load(open(DATA / "nogo.geojson"))
        return {f["properties"]["id"]: f for f in fc["features"]}

    def segments_in(self, feature, buffer_m=None):
        props = feature.get("properties", {})
        if buffer_m is None:
            buffer_m = props.get("buffer_m") or BUFFER_BY_KIND.get(props.get("kind"), NOGO_BUFFER_M)
        poly = transform(to_utm, shape(feature["geometry"])).buffer(buffer_m)
        return [self._ids[i] for i in self._tree.query(poly) if self._geoms_m[i].intersects(poly)]

    def unsafe_segments(self, active_ids):
        out = set()
        for nid in active_ids:
            out.update(self.segments_in(self.nogo[nid]))
        return out

    def nearest_segment(self, lon, lat, max_m=400):
        x, y = to_utm(lon, lat)
        from shapely.geometry import Point
        pt = Point(x, y)
        i = self._tree.nearest(pt)
        if self._geoms_m[i].distance(pt) > max_m:
            return None
        return self._ids[i]
