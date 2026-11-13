"""Deterministic residential parcel layouts for the Araçatexas world exporter."""
from __future__ import annotations

import hashlib
import math
import random
from typing import Any

from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union


def _seed(value: Any) -> int:
    return int.from_bytes(hashlib.sha256(str(value).encode("utf-8")).digest()[:4], "big")


def _xy(point):
    return [round(float(point[0]), 3), round(float(point[1]), 3)]


def _lot_geometry(lot):
    parts = []
    for rings in lot.get("polygons", []):
        if not rings or len(rings[0]) < 4:
            continue
        polygon = Polygon(rings[0], rings[1:])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty and polygon.area > 1:
            parts.append(polygon)
    if not parts:
        return None
    geometry = unary_union(parts)
    return geometry if geometry.is_valid else geometry.buffer(0)


def _road_records(map_data):
    """Return road centerlines and their mapped widths for safe parcel setbacks."""
    records = []
    for road in map_data.get("roads", []):
        width = max(4.0, float(road.get("width_m") or 7.0))
        for line in road.get("lines", []):
            if len(line) >= 2:
                records.append((LineString(line), width))
    return records


def _road_network(map_data):
    lines = [line for line, _ in _road_records(map_data)]
    return unary_union(lines) if lines else None


def _nearest_road_segment(point, road_records):
    candidates = []
    for line, width in road_records:
        coords = list(line.coords)
        for start, end in zip(coords, coords[1:]):
            segment = LineString([start, end])
            candidates.append((point.distance(segment), segment, width))
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _move_segment_inside(segment, geometry, road_records, extra_clearance=0.18):
    """Inset a road-adjacent parcel edge without moving a road-crossing side edge."""
    if not road_records or segment.is_empty:
        return segment
    midpoint = segment.interpolate(0.5, normalized=True)
    nearest = _nearest_road_segment(midpoint, road_records)
    if nearest is None:
        return segment
    distance, road_segment, width = nearest
    sx, sy = segment.coords[-1][0] - segment.coords[0][0], segment.coords[-1][1] - segment.coords[0][1]
    rx, ry = road_segment.coords[-1][0] - road_segment.coords[0][0], road_segment.coords[-1][1] - road_segment.coords[0][1]
    alignment = abs((sx * rx + sy * ry) / ((math.hypot(sx, sy) * math.hypot(rx, ry)) or 1.0))
    # A road crossing a parcel is not a frontage. Only inset edges parallel to a road.
    if alignment < 0.55:
        return segment

    clearance = width / 2 + 0.75
    offset = min(3.5, clearance - distance + extra_clearance)
    if offset <= 0:
        return segment

    center = geometry.representative_point()
    dx, dy = center.x - midpoint.x, center.y - midpoint.y
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return segment
    dx, dy = dx / norm, dy / norm

    for factor in (1.0, 0.75, 0.5, 0.25):
        candidate = translate(segment, xoff=dx * offset * factor, yoff=dy * offset * factor)
        if geometry.buffer(0.03).covers(candidate):
            return candidate
    return segment


def _frontage_edge(geometry, roads, road_records=None):
    exterior = max(
        (poly for poly in getattr(geometry, "geoms", [geometry]) if isinstance(poly, Polygon)),
        key=lambda poly: poly.area,
    ).exterior
    coords = list(exterior.coords)
    candidates = []
    for a, b in zip(coords, coords[1:]):
        segment = LineString([a, b])
        if segment.length < 2.5:
            continue
        midpoint = segment.interpolate(0.5, normalized=True)
        if road_records:
            nearest = _nearest_road_segment(midpoint, road_records)
            distance, road_segment, _ = nearest
            sx, sy = b[0] - a[0], b[1] - a[1]
            rx = road_segment.coords[-1][0] - road_segment.coords[0][0]
            ry = road_segment.coords[-1][1] - road_segment.coords[0][1]
            alignment = abs((sx * rx + sy * ry) / ((math.hypot(sx, sy) * math.hypot(rx, ry)) or 1.0))
            score = distance + (1.0 - alignment) * min(24.0, max(10.0, segment.length * 0.35))
        else:
            distance = midpoint.distance(roads) if roads is not None else float("inf")
            score = distance
        candidates.append((score, -segment.length, segment))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def _polygon_coordinates(geometry):
    if geometry is None or geometry.is_empty:
        return []
    polygons = [geometry] if isinstance(geometry, Polygon) else [
        part for part in getattr(geometry, "geoms", []) if isinstance(part, Polygon)
    ]
    result = []
    for polygon in polygons:
        rings = [[_xy(p) for p in polygon.exterior.coords]]
        rings.extend([[_xy(p) for p in ring.coords] for ring in polygon.interiors])
        result.append(rings)
    return result


def _wall_and_gate_layout(
    geometry, frontage, rng, house_geometry, road_records=(), source_frontage=None
):
    """Build a full parcel boundary, front access openings and a driveway/garage pad."""
    source_frontage = source_frontage or frontage
    a, b = list(frontage.coords)[0], list(frontage.coords)[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    tangent = (dx / length, dy / length)
    midpoint = frontage.interpolate(0.5, normalized=True)
    center = geometry.representative_point()
    inward = (center.x - midpoint.x, center.y - midpoint.y)
    norm = math.hypot(*inward) or 1.0
    inward = (inward[0] / norm, inward[1] / norm)

    garage_roll = rng.random()
    garage_type = "none" if garage_roll < 0.14 else "covered" if garage_roll < 0.54 else "uncovered"
    vehicle_width = min(3.2, length * 0.34) if length >= 7 and garage_type != "none" else 0.0
    pedestrian_width = min(1.4, length * 0.26) if length >= 2.5 else 0.0
    vehicle_t = 0.84 if rng.random() > 0.5 else 0.78
    pedestrian_t = 0.16 if vehicle_t > 0.8 else 0.88
    gaps = []
    gates = []
    for kind, t, width in (
        ("vehicle", vehicle_t, vehicle_width),
        ("pedestrian", pedestrian_t, pedestrian_width),
    ):
        if width <= 0:
            continue
        half = width / (2 * length)
        start_t, end_t = max(0.02, t - half), min(0.98, t + half)
        start = (a[0] + dx * start_t, a[1] + dy * start_t)
        end = (a[0] + dx * end_t, a[1] + dy * end_t)
        gaps.append((start_t, end_t))
        gates.append({"type": kind, "line": [_xy(start), _xy(end)]})

    walls = []
    for poly in getattr(geometry, "geoms", [geometry]):
        if not isinstance(poly, Polygon):
            continue
        coords = list(poly.exterior.coords)
        for p, q in zip(coords, coords[1:]):
            segment = LineString([p, q])
            # Exclude only the actual frontage edge, not side edges touching its endpoints.
            if segment.intersection(source_frontage).length > 0.02:
                continue
            segment = _move_segment_inside(segment, geometry, road_records)
            walls.append([_xy(segment.coords[0]), _xy(segment.coords[-1])])

    points = [0.0]
    for start_t, end_t in sorted(gaps):
        points.extend((start_t, end_t))
    points.append(1.0)
    for index in range(0, len(points) - 1, 2):
        start_t, end_t = points[index], points[index + 1]
        if end_t - start_t < 0.01:
            continue
        p = (a[0] + dx * start_t, a[1] + dy * start_t)
        q = (a[0] + dx * end_t, a[1] + dy * end_t)
        walls.append([_xy(p), _xy(q)])

    garage = None
    vehicle_gate = next((gate for gate in gates if gate["type"] == "vehicle"), None)
    if vehicle_gate:
        gate_a, gate_b = vehicle_gate["line"]
        gate_center = ((gate_a[0] + gate_b[0]) / 2, (gate_a[1] + gate_b[1]) / 2)
        width = min(2.9, max(2.4, length * 0.15))
        depth = rng.uniform(4.2, 5.2)
        cx = gate_center[0] + inward[0] * (depth / 2 + 0.45)
        cy = gate_center[1] + inward[1] * (depth / 2 + 0.45)
        tx, ty = tangent
        nx, ny = inward
        corners = [
            (cx - tx * width / 2 - nx * depth / 2, cy - ty * width / 2 - ny * depth / 2),
            (cx + tx * width / 2 - nx * depth / 2, cy + ty * width / 2 - ny * depth / 2),
            (cx + tx * width / 2 + nx * depth / 2, cy + ty * width / 2 + ny * depth / 2),
            (cx - tx * width / 2 + nx * depth / 2, cy - ty * width / 2 + ny * depth / 2),
        ]
        candidate = Polygon(corners)
        if geometry.covers(candidate) and (
            house_geometry is None or candidate.distance(house_geometry) >= 0.25
        ):
            garage = candidate
        else:
            garage_type = "none"

    return walls, gates, garage, inward, garage_type

def _new_house_geometry(geometry, frontage, rng):
    """Choose a deterministic lot-appropriate house placement/size profile."""
    coords = list(frontage.coords)
    a, b = coords[0], coords[-1]
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    origin = geometry.representative_point()
    aligned = rotate(geometry, -angle, origin=(origin.x, origin.y), use_radians=False)
    bounds = aligned.bounds
    min_x, min_y, max_x, max_y = bounds
    width, depth = max_x - min_x, max_y - min_y
    if min(width, depth) < 5.4:
        return None, angle, None, None

    frontage_mid = frontage.interpolate(0.5, normalized=True)
    front_local = rotate(frontage_mid, -angle, origin=(origin.x, origin.y), use_radians=False)
    front_is_min_y = abs(front_local.y - min_y) <= abs(front_local.y - max_y)

    # Layout controls position and footprint independently from architectural style.
    if width * depth >= 420 and depth >= 13:
        layout = rng.choices(
            ["front_house_back_yard", "rear_house_front_yard",
             "center_house_front_back", "large_house", "compact_house"],
            weights=[27, 20, 22, 16, 15],
            k=1,
        )[0]
    else:
        layout = rng.choices(
            ["front_house_back_yard", "center_house_front_back", "compact_house"],
            weights=[38, 30, 32],
            k=1,
        )[0]

    profiles = {
        "front_house_back_yard": ((0.58, 0.76), (0.40, 0.54), (1.8, 3.2), (0.30, 0.46)),
        "rear_house_front_yard": ((0.56, 0.73), (0.40, 0.55), (0.30, 0.46), (1.6, 2.8)),
        "center_house_front_back": ((0.56, 0.73), (0.40, 0.54), (0.20, 0.29), (0.20, 0.29)),
        "large_house": ((0.76, 0.90), (0.64, 0.78), (1.6, 2.6), (1.2, 2.2)),
        "compact_house": ((0.40, 0.57), (0.32, 0.46), (2.0, 3.2), (0.28, 0.44)),
    }
    width_range, depth_range, front_range, rear_range = profiles[layout]
    house_width = width * rng.uniform(*width_range)
    side_margin = max(0.65, min(1.6, width * 0.08))
    house_width = min(house_width, width - side_margin * 2)

    front_setback = (
        rng.uniform(*front_range) * depth
        if front_range[1] < 1
        else rng.uniform(*front_range)
    )
    rear_setback = (
        rng.uniform(*rear_range) * depth
        if rear_range[1] < 1
        else rng.uniform(*rear_range)
    )
    # Preserve usable clearances even on unusually shallow parcels.
    max_house_depth = depth - front_setback - rear_setback
    if max_house_depth < 4.0:
        front_setback = max(1.2, depth * 0.15)
        rear_setback = max(1.0, depth * 0.12)
        max_house_depth = depth - front_setback - rear_setback
    if max_house_depth < 3.8:
        return None, angle, None, layout

    house_depth = min(depth * rng.uniform(*depth_range), max_house_depth)
    house_depth = max(3.8, house_depth)
    if house_depth > max_house_depth:
        house_depth = max_house_depth

    style_roll = rng.random()
    kind = (
        "sobrado" if style_roll < 0.17 else
        "casa_antiga" if style_roll < 0.34 else
        "casa_estendida" if style_roll < 0.56 else
        "casa_terrea"
    )
    # Bias the footprint slightly left so a right-side vehicle gate can reach a driveway.
    cx = (min_x + max_x) / 2 + rng.uniform(-0.13, -0.07) * width
    if front_is_min_y:
        cy = min_y + front_setback + house_depth / 2
    else:
        cy = max_y - front_setback - house_depth / 2

    candidate = None
    for _ in range(14):
        local = box(
            cx - house_width / 2, cy - house_depth / 2,
            cx + house_width / 2, cy + house_depth / 2,
        )
        if aligned.covers(local):
            candidate = rotate(local, angle, origin=(origin.x, origin.y), use_radians=False)
            break
        house_width *= 0.94
        house_depth *= 0.96
    if candidate is None:
        inner = aligned.buffer(-0.65)
        if not inner.is_empty:
            candidate_local = inner.intersection(box(
                cx - width * 0.42, cy - depth * 0.42,
                cx + width * 0.42, cy + depth * 0.42,
            ))
            if isinstance(candidate_local, Polygon) and candidate_local.area >= 22:
                candidate = rotate(
                    candidate_local, angle, origin=(origin.x, origin.y), use_radians=False
                )
    if candidate is None or candidate.is_empty or candidate.area < 22:
        return None, angle, kind, layout
    return candidate, angle, kind, layout

def _oriented_dimensions(geometry):
    if geometry is None or geometry.is_empty:
        return 0.0, 0.0
    rectangle = geometry.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    lengths = [
        math.hypot(coords[i + 1][0] - coords[i][0], coords[i + 1][1] - coords[i][1])
        for i in range(4)
    ]
    return max(lengths), min(lengths)


def _boundary_clearances(geometry, frontage, house_geometry, inward):
    if house_geometry is None:
        return 0.0, 0.0
    largest = max(
        (poly for poly in getattr(geometry, "geoms", [geometry]) if isinstance(poly, Polygon)),
        key=lambda poly: poly.area,
    )
    edges = []
    coords = list(largest.exterior.coords)
    front_mid = frontage.interpolate(0.5, normalized=True)
    for a, b in zip(coords, coords[1:]):
        edge = LineString([a, b])
        # Endpoint contact is not enough to classify a side/rear edge as frontage.
        if edge.intersection(frontage).length > 0.02:
            continue
        midpoint = edge.interpolate(0.5, normalized=True)
        projection = (
            (midpoint.x - front_mid.x) * inward[0]
            + (midpoint.y - front_mid.y) * inward[1]
        )
        edges.append((projection, edge))
    if not edges:
        return 0.0, 0.0
    rear = max(edges, key=lambda item: item[0])[1]
    side_edges = [edge for _, edge in edges if edge is not rear]
    side_clearance = min(
        (house_geometry.distance(edge) for edge in side_edges), default=0.0
    )
    return float(side_clearance), float(house_geometry.distance(rear))

def generate_residential_properties(map_data: dict, seed: int) -> dict:
    """Add varied deterministic residential layouts to suitable parcels."""
    road_records = _road_records(map_data)
    roads = unary_union([line for line, _ in road_records]) if road_records else None
    by_lot = {}
    for building in map_data.get("buildings", []):
        lot_id = building.get("lot_id")
        if lot_id:
            by_lot.setdefault(str(lot_id), []).append(building)

    occupied_footprints = []
    for building in map_data.get("buildings", []):
        for rings in building.get("polygons", []):
            if not rings or len(rings[0]) < 4:
                continue
            try:
                footprint = Polygon(rings[0], rings[1:])
                if not footprint.is_valid:
                    footprint = footprint.buffer(0)
                if not footprint.is_empty:
                    occupied_footprints.append((str(building.get("id", "")), footprint))
            except Exception:
                continue

    generated = []
    property_count = 0
    skipped = 0
    shared_wall_count = 0
    seen_wall_segments = set()
    layout_counts = {}
    garage_counts = {"covered": 0, "uncovered": 0, "none": 0}

    for lot in map_data.get("lots", []):
        lot_id = str(lot.get("id", ""))
        zone = str(lot.get("zone", "residential")).lower()
        all_existing = by_lot.get(lot_id, [])
        residential_existing = [
            item for item in all_existing
            if str(item.get("building_type", "house")).lower()
            in {"house", "residential", "yes"}
            and not any(
                word in str(item.get("name", "")).lower()
                for word in ("shopping", "posto", "restaurante", "supermercado", "loja")
            )
        ]
        is_residential_lot = zone == "residential" or (zone == "mixed" and residential_existing)
        if all_existing and not residential_existing:
            continue
        existing = list(residential_existing)
        if not is_residential_lot:
            continue

        geometry = _lot_geometry(lot)
        if geometry is None or geometry.area < 75:
            skipped += 1
            continue
        frontage = _frontage_edge(geometry, roads, road_records)
        if frontage is None:
            skipped += 1
            continue

        lot_rng = random.Random(_seed(f"{seed}:{lot_id}:residence"))
        safe_frontage = _move_segment_inside(frontage, geometry, road_records)
        house_geometry = None
        existing_procedural = next(
            (item for item in existing if item.get("source") == "procedural-residential-lot"),
            None,
        )
        previous_property = lot.get("residential") or {}
        house_kind = (
            previous_property.get("type")
            if previous_property.get("generated")
            else (existing_procedural.get("visual_style") or {}).get("residence_type", "existing")
            if existing_procedural else "existing"
        )
        layout_type = (
            previous_property.get("layout")
            if previous_property.get("generated")
            else "existing"
        )
        generated_here = bool(existing_procedural or previous_property.get("generated"))

        # Leave a small deterministic share of otherwise vacant lots empty.
        if (
            not all_existing and zone == "residential" and geometry.area >= 105
            and lot_rng.random() < 0.76
        ):
            house_geometry, angle, house_kind, layout_type = _new_house_geometry(
                geometry, safe_frontage, lot_rng
            )
            if house_geometry is not None and any(
                house_geometry.intersection(other).area > 0.5
                for _, other in occupied_footprints
            ):
                house_geometry = None
                skipped += 1
            if house_geometry is not None:
                building_id = f"proc_house_{lot_id}"
                occupied_footprints.append((building_id, house_geometry))
                coords = _polygon_coordinates(house_geometry)
                variant_seed = _seed(f"{seed}:{lot_id}:house")
                building = {
                    "id": building_id,
                    "lot_id": lot_id,
                    "block_id": lot.get("block_id"),
                    "building_type": "house",
                    "source": "procedural-residential-lot",
                    "name": "",
                    "area_m2": round(float(house_geometry.area), 1),
                    "fixed": False,
                    "visual_style": {
                        "category": "residential",
                        "variant": variant_seed % 4,
                        "seed": variant_seed,
                        "residence_type": house_kind,
                    },
                    "polygons": coords,
                }
                map_data.setdefault("buildings", []).append(building)
                by_lot[lot_id] = [building]
                existing = [building]
                generated.append(building_id)
                generated_here = True

        if not existing:
            # No structure is intentional for some residential parcels.
            skipped += 1
            continue

        if house_geometry is None:
            candidate = next((item for item in existing if item.get("polygons")), None)
            if candidate:
                polygons = candidate.get("polygons", [])
                if polygons and polygons[0] and polygons[0][0]:
                    try:
                        house_geometry = Polygon(polygons[0][0], polygons[0][1:])
                        if not house_geometry.is_valid:
                            house_geometry = house_geometry.buffer(0)
                    except Exception:
                        house_geometry = None

        walls, gates, garage, inward, garage_type = _wall_and_gate_layout(
            geometry, safe_frontage, lot_rng, house_geometry,
            road_records, source_frontage=frontage,
        )

        unique_walls = []
        for wall in walls:
            key = tuple(sorted(
                tuple(round(float(value), 2) for value in point) for point in wall
            ))
            if key in seen_wall_segments:
                shared_wall_count += 1
                continue
            seen_wall_segments.add(key)
            unique_walls.append(wall)

        house_area = float(house_geometry.area) if house_geometry is not None else 0.0
        garage_area = float(garage.area) if garage is not None else 0.0
        yard_geometry = geometry
        if house_geometry is not None:
            yard_geometry = yard_geometry.difference(house_geometry)
        if garage is not None:
            yard_geometry = yard_geometry.difference(garage)
        yard_geometry = yard_geometry.buffer(0)
        yard_area = max(0.0, float(yard_geometry.area))
        yard_ratio = yard_area / max(float(geometry.area), 1.0)
        yard_class = "large" if yard_ratio > 0.48 else "medium" if yard_ratio > 0.25 else "small"
        yard_surface = lot_rng.choices(
            ["grass", "garden", "dirt"], weights=[72, 20, 8], k=1
        )[0]
        wall_type = lot_rng.choice(["plaster", "brick", "concrete"])
        house_bounds = house_geometry.bounds if house_geometry is not None else geometry.bounds
        house_length, house_width = _oriented_dimensions(house_geometry)
        side_clearance, rear_clearance = _boundary_clearances(
            geometry, frontage, house_geometry, inward
        )

        lot["residential"] = {
            "version": 2,
            "generated": generated_here,
            "type": house_kind,
            "layout": layout_type or "existing",
            "wall_type": wall_type,
            "frontage": [_xy(safe_frontage.coords[0]), _xy(safe_frontage.coords[-1])],
            "parcel_frontage": [_xy(frontage.coords[0]), _xy(frontage.coords[-1])],
            "walls": unique_walls,
            "gates": gates,
            "garage": _polygon_coordinates(garage) if garage is not None else [],
            "garage_type": garage_type,
            "garage_roof": _polygon_coordinates(garage) if garage is not None and garage_type == "covered" else [],
            "garage_area_m2": round(garage_area, 1),
            "yard": {
                "area_m2": round(yard_area, 1),
                "class": yard_class,
                "surface": yard_surface,
                "polygons": _polygon_coordinates(yard_geometry),
            },
            "house": {
                "building_ids": [str(item.get("id")) for item in existing],
                "length_m": round(house_length, 2),
                "width_m": round(house_width, 2),
                "bounds_width_m": round(float(house_bounds[2] - house_bounds[0]), 2),
                "bounds_depth_m": round(float(house_bounds[3] - house_bounds[1]), 2),
                "area_m2": round(house_area, 1),
            },
            "setbacks_m": {
                "front": round(max(0.0, safe_frontage.distance(house_geometry)) if house_geometry else 0.0, 2),
                "side": round(max(0.0, side_clearance), 2),
                "rear": round(max(0.0, rear_clearance), 2),
            },
        }
        property_count += 1
        layout_counts[layout_type or "existing"] = layout_counts.get(layout_type or "existing", 0) + 1
        garage_counts[garage_type] = garage_counts.get(garage_type, 0) + 1

    return {
        "properties": property_count,
        "generated_houses": len(generated),
        "generated_building_ids": generated,
        "skipped_lots": skipped,
        "shared_wall_segments_removed": shared_wall_count,
        "layout_counts": layout_counts,
        "garage_counts": garage_counts,
    }
