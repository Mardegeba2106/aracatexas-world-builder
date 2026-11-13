"""Procedural pixel-art world exporter for Araçatexas.

Consumes the prepared Araçatexas map.json (local UTM 22S metres) and creates:
- a deterministic pixel-art preview,
- control maps (one pixel per 2 m world tile),
- a 32x32 ground/road atlas and a modular building-atlas starter,
- a game-compatible map JSON with deterministic residential houses, property walls/gates/garages and generated trees,
- a chunk index and a distributable ZIP.

This module is independent from the legacy Knoxify / Project Zomboid renderer.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import zipfile
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw

from generator.residential import generate_residential_properties

TILE_SIZE = 32
TILE_METERS = 2.0
CHUNK_TILES = 64
PREVIEW_PIXELS_PER_METER = 2.0
PREVIEW_MAX_EDGE = 2800
SEED = 20261108

PALETTE = {
    "grass": (63, 91, 57),
    "grass_light": (76, 105, 63),
    "grass_dark": (48, 73, 49),
    "lot_residential": (91, 113, 70),
    "lot_commercial": (111, 112, 83),
    "lot_mixed": (119, 111, 75),
    "sidewalk": (137, 137, 122),
    "sidewalk_light": (160, 158, 139),
    "curb": (190, 184, 157),
    "asphalt": (57, 62, 61),
    "asphalt_light": (69, 73, 70),
    "road_edge": (37, 45, 41),
    "marking": (220, 213, 177),
    "water": (55, 112, 139),
    "tree_dark": (31, 65, 42),
    "tree_mid": (51, 99, 55),
    "tree_light": (89, 132, 68),
}

ROOF_PALETTES = {
    "house": [(145, 76, 57), (157, 88, 61), (126, 78, 61), (116, 105, 86)],
    "residential": [(148, 79, 59), (159, 94, 64), (124, 86, 69), (111, 111, 101)],
    "commercial": [(91, 111, 113), (105, 121, 119), (127, 117, 98), (82, 101, 105)],
    "industrial": [(108, 112, 105), (125, 127, 117), (92, 103, 101)],
    "default": [(143, 83, 62), (133, 106, 83), (105, 112, 103)],
}


def _stable_int(value: Any) -> int:
    digest = hashlib.sha256(str(value).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _iter_geometry_points(map_data: dict) -> Iterable[tuple[float, float]]:
    for layer in ("blocks", "lots", "buildings"):
        for feature in map_data.get(layer, []):
            for polygon in feature.get("polygons", []):
                for ring in polygon:
                    for point in ring:
                        if isinstance(point, (list, tuple)) and len(point) >= 2:
                            yield float(point[0]), float(point[1])
    for road in map_data.get("roads", []):
        for line in road.get("lines", []):
            for point in line:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    yield float(point[0]), float(point[1])
    for tree in map_data.get("trees", []):
        point = tree.get("point")
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            yield float(point[0]), float(point[1])


def validate_map(map_data: Any) -> dict:
    if not isinstance(map_data, dict):
        raise ValueError("O arquivo precisa conter um objeto JSON.")
    meta = map_data.get("meta")
    if not isinstance(meta, dict) or meta.get("crs") != "EPSG:32722":
        raise ValueError("O mapa precisa usar EPSG:32722 e ser exportado pelo prepare_map.py do Araçatexas.")
    for layer in ("blocks", "lots", "buildings", "roads"):
        if not isinstance(map_data.get(layer), list):
            raise ValueError(f"A camada obrigatória '{layer}' não existe ou não é uma lista.")
    if not map_data["roads"]:
        raise ValueError("O mapa não contém ruas. Use o public/data/map.json do Araçatexas Game.")
    if not any(feature.get("polygons") for feature in map_data["buildings"]):
        raise ValueError("O mapa não contém footprints de construções.")
    return map_data


def _world_bounds(map_data: dict) -> tuple[float, float, float, float]:
    points = list(_iter_geometry_points(map_data))
    if not points:
        raise ValueError("O mapa não contém geometrias para desenhar.")
    radius = float(map_data.get("meta", {}).get("radius_m") or 0)
    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    max_x = max(p[0] for p in points)
    max_y = max(p[1] for p in points)
    if radius > 0:
        min_x, max_x = min(min_x, -radius), max(max_x, radius)
        min_y, max_y = min(min_y, -radius), max(max_y, radius)
    return min_x, min_y, max_x, max_y


def _rings_to_pixels(polygons, transform):
    output = []
    for polygon in polygons or []:
        rings = []
        for ring in polygon:
            points = [transform(p) for p in ring if len(p) >= 2]
            if len(points) >= 3:
                rings.append(points)
        if rings:
            output.append(rings)
    return output


def _centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    if len(ring) < 3:
        return None
    area2 = cx = cy = 0.0
    pairs = zip(ring, ring[1:] + ring[:1])
    for a, b in pairs:
        cross = a[0] * b[1] - b[0] * a[1]
        area2 += cross
        cx += (a[0] + b[0]) * cross
        cy += (a[1] + b[1]) * cross
    if abs(area2) < 1e-8:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    return cx / (3 * area2), cy / (3 * area2)


def _point_in_ring(point: tuple[float, float], ring: list[list[float]]) -> bool:
    x, y = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _draw_noise(image: Image.Image, seed: int, density: float = 0.003) -> None:
    rng = random.Random(seed)
    draw = ImageDraw.Draw(image)
    count = max(1, int(image.width * image.height * density))
    colors = [PALETTE["grass_dark"], PALETTE["grass_light"], (70, 96, 60)]
    for _ in range(count):
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        c = colors[rng.randrange(len(colors))]
        draw.rectangle((x, y, x + rng.choice((0, 1)), y + rng.choice((0, 1))), fill=c)


def _road_kind(road: dict) -> str:
    highway = str(road.get("highway", "residential")).lower()
    if highway in {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}:
        return "arterial"
    if highway in {"secondary", "secondary_link", "tertiary", "tertiary_link"}:
        return "collector"
    return "local"


def _draw_road_texture(draw: ImageDraw.ImageDraw, line: list[tuple[float, float]], width: float,
                       seed: int, arterial: bool = False) -> None:
    if len(line) < 2:
        return
    rng = random.Random(seed)
    step = 7 if width > 10 else 9
    for a, b in zip(line, line[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1:
            continue
        nx, ny = -dy / length, dx / length
        count = max(1, int(length / step))
        for n in range(count):
            t = (n + 0.5) / count
            x = a[0] + dx * t
            y = a[1] + dy * t
            for _ in range(1 if width < 10 else 2):
                offset = rng.uniform(-max(1.0, width * 0.38), max(1.0, width * 0.38))
                px, py = int(x + nx * offset), int(y + ny * offset)
                shade = rng.choice(((75, 79, 75), (48, 54, 53), (62, 68, 65)))
                draw.rectangle((px, py, px + 1, py + 1), fill=shade)


def _draw_roof(draw: ImageDraw.ImageDraw, polygon: list[list[tuple[float, float]]],
               feature: dict, seed: int) -> None:
    if not polygon:
        return
    outer = polygon[0]
    if len(outer) < 3:
        return
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    kind = str(feature.get("building_type", "house")).lower()
    commercial = any(tag in kind for tag in ("commercial", "retail", "shop", "office"))
    industrial = any(tag in kind for tag in ("industrial", "warehouse", "factory"))
    palette_key = "industrial" if industrial else "commercial" if commercial else "house"
    colors = ROOF_PALETTES[palette_key]
    roof = colors[seed % len(colors)]
    dark = tuple(max(0, v - 34) for v in roof)
    light = tuple(min(255, v + 32) for v in roof)

    # Beiral/sombra deslocados para baixo, preservando a projeção top-down.
    shadow = [(int(x + 3), int(y + 4)) for x, y in outer]
    draw.polygon(shadow, fill=(28, 38, 31))
    draw.polygon(outer, fill=dark)
    inner = [(int(x), int(y)) for x, y in outer]
    draw.polygon(inner, fill=roof)
    draw.line(inner + [inner[0]], fill=light, width=1)

    # Textura de telha/metal em linhas curtas, determinística e recortada pelo footprint.
    rng = random.Random(seed)
    spacing = 5 if (max_y - min_y) > 12 else 4
    for y in range(int(min_y + spacing), int(max_y - 1), spacing):
        intersections = []
        for a, b in zip(outer[-1:] + outer[:-1], outer):
            if (a[1] > y) != (b[1] > y):
                intersections.append(a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]))
        intersections.sort()
        for left, right in zip(intersections[::2], intersections[1::2]):
            if right - left > 4:
                offset = rng.randint(0, 2)
                draw.line((int(left + 2), y + offset, int(right - 2), y + offset), fill=dark, width=1)
                if rng.random() > 0.55:
                    x = int(rng.uniform(left + 2, right - 2))
                    draw.point((x, y + 1), fill=light)

    # Pequena claraboia/caixa técnica. Fachada no lado sul do footprint.
    cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
    if max_x - min_x > 12 and max_y - min_y > 10:
        draw.rectangle((int(cx - 2), int(cy - 2), int(cx + 2), int(cy + 1)), fill=(191, 183, 151), outline=dark)
    south = sorted(outer, key=lambda p: p[1], reverse=True)[:2]
    if len(south) == 2:
        draw.line((south[0][0], south[0][1], south[1][0], south[1][1]), fill=(45, 42, 36), width=2)


def _draw_tree(draw: ImageDraw.ImageDraw, x: float, y: float, seed: int, scale: float = 1.0) -> None:
    rng = random.Random(seed)
    radius = max(3, int((4 + seed % 4) * scale))
    draw.ellipse((x - radius + 2, y - radius + 4, x + radius + 3, y + radius + 6), fill=(30, 43, 32))
    draw.rectangle((x - 1, y - 1, x + 1, y + radius), fill=(98, 70, 43))
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=PALETTE["tree_dark"])
    draw.ellipse((x - radius + 2, y - radius + 1, x + radius - 2, y + radius - 2), fill=PALETTE["tree_mid"])
    draw.rectangle((x - 2, y - radius + 2, x + 1, y - radius + 4), fill=PALETTE["tree_light"])
    for _ in range(2):
        px = rng.randint(int(x - radius + 2), int(x + radius - 2))
        py = rng.randint(int(y - radius + 2), int(y + radius - 2))
        draw.point((px, py), fill=PALETTE["tree_light"])


def _generate_empty_lot_trees(map_data: dict, seed: int) -> list[dict]:
    occupied_lots = {str(b.get("lot_id")) for b in map_data.get("buildings", []) if b.get("lot_id")}
    result = []
    for lot in map_data.get("lots", []):
        lot_id = str(lot.get("id", ""))
        lot_seed = _stable_int(f"{seed}:{lot_id}")
        rng = random.Random(lot_seed)
        if lot_id in occupied_lots or rng.random() > 0.26:
            continue
        for polygon in lot.get("polygons", []):
            if not polygon:
                continue
            center = _centroid(polygon[0])
            if center and _point_in_ring(center, polygon[0]):
                result.append({
                    "id": f"proc_tree_{lot_id or len(result)}",
                    "point": [round(center[0], 3), round(center[1], 3)],
                    "source": "procedural-empty-lot",
                    "kind": "tree",
                })
                break
    return result


def _make_tileset(path: Path, seed: int) -> dict:
    cols, rows = 8, 4
    image = Image.new("RGBA", (cols * TILE_SIZE, rows * TILE_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    definitions = []
    tiles = [
        ("grass_01", "grass"), ("grass_02", "grass"), ("grass_03", "grass"), ("grass_04", "grass"),
        ("dirt", "ground"), ("park_grass", "ground"), ("water", "ground"), ("plaza", "ground"),
        ("asphalt_01", "road"), ("asphalt_02", "road"), ("asphalt_03", "road"), ("asphalt_arterial", "road"),
        ("sidewalk_01", "sidewalk"), ("sidewalk_02", "sidewalk"), ("curb", "sidewalk"), ("crosswalk", "marking"),
        ("road_horizontal", "road"), ("road_vertical", "road"), ("intersection", "road"), ("road_corner", "road"),
        ("lot_residential", "lot"), ("lot_commercial", "lot"), ("roof_red", "building"), ("roof_gray", "building"),
        ("wall_warm", "building"), ("wall_cool", "building"), ("tree_top", "vegetation"), ("shrub", "vegetation"),
        ("parking", "marking"), ("lane_dash", "marking"), ("paving", "ground"), ("concrete", "ground"),
    ]
    for index, (name, category) in enumerate(tiles):
        x, y = (index % cols) * TILE_SIZE, (index // cols) * TILE_SIZE
        tile = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        rng = random.Random(_stable_int(f"{seed}:{name}"))
        if name.startswith("grass") or name in {"park_grass", "lot_residential", "lot_commercial"}:
            base = PALETTE["grass"] if category == "grass" else PALETTE["lot_commercial"] if "commercial" in name else PALETTE["lot_residential"]
            tile = Image.new("RGBA", (32, 32), base + (255,)); td = ImageDraw.Draw(tile)
            for _ in range(27):
                px, py = rng.randrange(32), rng.randrange(32)
                td.point((px, py), fill=rng.choice((PALETTE["grass_light"], PALETTE["grass_dark"])))
        elif name == "dirt":
            tile = Image.new("RGBA", (32, 32), (123, 103, 74, 255)); td = ImageDraw.Draw(tile)
            for _ in range(24): td.point((rng.randrange(32), rng.randrange(32)), fill=(145, 122, 88, 255))
        elif name == "water":
            tile = Image.new("RGBA", (32, 32), PALETTE["water"] + (255,)); td = ImageDraw.Draw(tile)
            for yy in (7, 18, 26): td.line((3, yy, 13, yy), fill=(99, 157, 171, 255), width=1)
        elif name.startswith("asphalt") or name in {"road_horizontal", "road_vertical", "intersection", "road_corner"}:
            color = (48, 54, 53, 255) if "arterial" in name else (57, 62, 61, 255)
            tile = Image.new("RGBA", (32, 32), color); td = ImageDraw.Draw(tile)
            for _ in range(32): td.point((rng.randrange(32), rng.randrange(32)), fill=rng.choice(((72, 77, 74, 255), (44, 49, 48, 255))))
            if name == "road_horizontal": td.line((2, 16, 30, 16), fill=PALETTE["marking"] + (230,), width=1)
            if name == "road_vertical": td.line((16, 2, 16, 30), fill=PALETTE["marking"] + (230,), width=1)
            if name == "intersection":
                td.line((0, 16, 32, 16), fill=(74, 78, 74, 255), width=1); td.line((16, 0, 16, 32), fill=(74, 78, 74, 255), width=1)
        elif name.startswith("sidewalk") or name == "curb":
            tile = Image.new("RGBA", (32, 32), PALETTE["sidewalk"] + (255,)); td = ImageDraw.Draw(tile)
            for yy in range(0, 32, 8): td.line((0, yy, 32, yy), fill=(111, 116, 107, 255), width=1)
            if name == "curb": td.line((1, 0, 1, 32), fill=PALETTE["curb"] + (255,), width=3)
        elif name == "crosswalk":
            tile = Image.new("RGBA", (32, 32), (50, 56, 54, 255)); td = ImageDraw.Draw(tile)
            for xx in range(2, 32, 7): td.rectangle((xx, 2, xx + 3, 29), fill=(222, 215, 183, 255))
        elif name == "parking":
            tile = Image.new("RGBA", (32, 32), (55, 60, 59, 255)); td = ImageDraw.Draw(tile)
            td.line((5, 2, 5, 29), fill=(220, 213, 177, 255), width=1); td.line((26, 2, 26, 29), fill=(220, 213, 177, 255), width=1)
        elif name == "lane_dash":
            tile = Image.new("RGBA", (32, 32), (55, 60, 59, 255)); td = ImageDraw.Draw(tile)
            td.line((2, 16, 13, 16), fill=(220, 213, 177, 255), width=2)
        elif name in {"roof_red", "roof_gray"}:
            color = (148, 79, 59, 255) if name == "roof_red" else (105, 112, 105, 255)
            tile = Image.new("RGBA", (32, 32), color); td = ImageDraw.Draw(tile)
            for yy in range(5, 32, 6): td.line((2, yy, 29, yy), fill=(70, 61, 52, 255), width=1)
        elif name.startswith("wall"):
            color = (173, 135, 102, 255) if name == "wall_warm" else (124, 143, 146, 255)
            tile = Image.new("RGBA", (32, 32), color); td = ImageDraw.Draw(tile)
            td.rectangle((5, 7, 11, 17), fill=(60, 91, 100, 255), outline=(218, 204, 171, 255))
            td.rectangle((21, 7, 27, 17), fill=(60, 91, 100, 255), outline=(218, 204, 171, 255))
        elif name == "tree_top":
            tile = Image.new("RGBA", (32, 32), (0, 0, 0, 0)); td = ImageDraw.Draw(tile)
            td.ellipse((3, 5, 29, 29), fill=PALETTE["tree_dark"] + (255,)); td.ellipse((8, 4, 25, 22), fill=PALETTE["tree_mid"] + (255,)); td.point((13, 10), fill=PALETTE["tree_light"] + (255,))
        elif name == "shrub":
            tile = Image.new("RGBA", (32, 32), (0, 0, 0, 0)); td = ImageDraw.Draw(tile)
            td.ellipse((5, 10, 26, 25), fill=PALETTE["tree_dark"] + (255,)); td.ellipse((10, 8, 21, 21), fill=PALETTE["tree_light"] + (255,))
        else:
            tile = Image.new("RGBA", (32, 32), (130, 130, 118, 255)); td = ImageDraw.Draw(tile)
            for yy in range(0, 32, 8): td.line((0, yy, 32, yy), fill=(108, 110, 101, 255), width=1)
        image.alpha_composite(tile, (x, y))
        definitions.append({"id": index, "name": name, "category": category, "x": x, "y": y, "width": TILE_SIZE, "height": TILE_SIZE})
    image.save(path)
    return {"file": path.name, "tile_size": TILE_SIZE, "columns": cols, "rows": rows, "tiles": definitions}


def _make_building_atlas(path: Path, seed: int) -> dict:
    cell = 64
    cols, rows = 4, 2
    image = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    variants = [
        ("casa_terrea", (153, 82, 59), (188, 151, 113)),
        ("casa_telhado_cinza", (113, 116, 106), (178, 163, 137)),
        ("sobrado", (143, 78, 62), (196, 170, 134)),
        ("comercio", (82, 108, 111), (183, 176, 147)),
        ("predio_residencial", (112, 122, 115), (190, 182, 154)),
        ("galpao", (119, 125, 117), (150, 151, 137)),
        ("loja_esquina", (102, 117, 112), (209, 177, 120)),
        ("fachada_estreita", (151, 92, 63), (198, 164, 123)),
    ]
    entries = []
    for index, (name, roof, wall) in enumerate(variants):
        x, y = (index % cols) * cell, (index // cols) * cell
        rng = random.Random(_stable_int(f"{seed}:prefab:{name}"))
        # Shadow, facade depth, rooftop and a small front-facing wall strip.
        d.polygon([(x+8,y+16),(x+49,y+9),(x+57,y+43),(x+16,y+52)], fill=(27,36,29,210))
        d.polygon([(x+7,y+12),(x+47,y+7),(x+55,y+39),(x+15,y+47)], fill=tuple(max(0,c-35) for c in roof)+(255,))
        d.polygon([(x+10,y+13),(x+45,y+9),(x+52,y+36),(x+16,y+42)], fill=roof+(255,))
        for yy in range(y+17, y+38, 5):
            d.line((x+13,yy,x+49,yy-4), fill=tuple(max(0,c-25) for c in roof)+(255,), width=1)
        d.polygon([(x+16,y+42),(x+52,y+36),(x+52,y+43),(x+16,y+50)], fill=wall+(255,))
        # Windows and door on the visible lower façade.
        d.rectangle((x+21,y+41,x+27,y+46), fill=(52,83,91,255), outline=(229,211,171,255))
        d.rectangle((x+37,y+38,x+43,y+43), fill=(52,83,91,255), outline=(229,211,171,255))
        d.rectangle((x+30,y+43,x+35,y+50), fill=(89,61,43,255), outline=(218,195,150,255))
        if name in {"comercio", "loja_esquina"}:
            d.rectangle((x+17,y+32,x+48,y+36), fill=(214,190,132,255))
            for stripe in range(x+19,x+47,7): d.line((stripe,y+32,stripe,y+36), fill=(151,72,55,255), width=1)
        if name == "sobrado":
            d.rectangle((x+12,y+19,x+21,y+26), fill=(63,88,93,255), outline=(222,209,177,255))
            d.rectangle((x+34,y+16,x+43,y+23), fill=(63,88,93,255), outline=(222,209,177,255))
        for _ in range(4):
            px, py = rng.randrange(x+12,x+51), rng.randrange(y+14,y+37)
            d.point((px,py), fill=tuple(min(255,c+25) for c in roof)+(255,))
        entries.append({"id":index,"name":name,"x":x,"y":y,"width":cell,"height":cell})
    image.save(path)
    return {"file":path.name,"cell_size":cell,"columns":cols,"rows":rows,"prefabs":entries}


def _control_images(map_data: dict, output_dir: Path, bounds, pixels_per_tile=1.0) -> dict:
    min_x, min_y, max_x, max_y = bounds
    width = max(1, int(math.ceil((max_x - min_x) / TILE_METERS)))
    height = max(1, int(math.ceil((max_y - min_y) / TILE_METERS)))
    # Prevent hostile/accidental input from creating enormous control rasters.
    if width * height > 9_000_000:
        raise ValueError("O mapa é grande demais para rasterizar em uma única imagem de controle.")
    size = (width, height)
    transform = lambda p: (int((p[0] - min_x) / TILE_METERS), int((max_y - p[1]) / TILE_METERS))
    ground = Image.new("RGB", size, PALETTE["grass"])
    zones = Image.new("RGB", size, (53, 99, 66))
    buildings = Image.new("RGB", size, (0, 0, 0))
    roads = Image.new("RGB", size, (0, 0, 0))
    gd, zd, bd, rd = map(ImageDraw.Draw, (ground, zones, buildings, roads))

    for block in map_data.get("blocks", []):
        zone = str(block.get("zone", "residential"))
        color = (174, 139, 75) if zone == "mixed" else (183, 116, 72) if zone == "commercial" else (73, 121, 70)
        for poly in _rings_to_pixels(block.get("polygons"), transform):
            if poly: zd.polygon(poly[0], fill=color)
    for lot in map_data.get("lots", []):
        zone = str(lot.get("zone", "residential"))
        fill = (116, 133, 83) if zone == "residential" else (156, 130, 82) if zone == "mixed" else (159, 121, 79)
        for poly in _rings_to_pixels(lot.get("polygons"), transform):
            if poly:
                gd.polygon(poly[0], fill=fill)
                gd.line(poly[0] + [poly[0][0]], fill=(95, 109, 73), width=1)
    for road in map_data.get("roads", []):
        width_px = max(1, int(float(road.get("width_m") or 7) / TILE_METERS))
        for line in road.get("lines", []):
            pts = [transform(p) for p in line]
            if len(pts) > 1:
                rd.line(pts, fill=(255, 255, 255), width=width_px, joint="curve")
                gd.line(pts, fill=PALETTE["asphalt"], width=width_px + 2, joint="curve")
    for building in map_data.get("buildings", []):
        kind = str(building.get("building_type", "house")).lower()
        color = (220, 105, 80) if any(t in kind for t in ("commercial", "retail", "shop", "office")) else (145, 151, 157)
        for poly in _rings_to_pixels(building.get("polygons"), transform):
            if poly: bd.polygon(poly[0], fill=color)
    paths = {}
    for name, image in (("ground", ground), ("zones", zones), ("roads", roads), ("buildings", buildings)):
        filename = f"control_{name}.png"
        image.save(output_dir / filename, optimize=True)
        paths[name] = {"file": filename, "width": width, "height": height, "meters_per_pixel": TILE_METERS}
    return paths


def _chunk_index(bounds, feature_layers: dict) -> dict:
    min_x, min_y, max_x, max_y = bounds
    span = TILE_METERS * CHUNK_TILES
    start_x = math.floor(min_x / span)
    end_x = math.floor(max_x / span)
    start_y = math.floor(min_y / span)
    end_y = math.floor(max_y / span)
    chunks = []
    for cy in range(start_y, end_y + 1):
        for cx in range(start_x, end_x + 1):
            left, bottom = cx * span, cy * span
            right, top = left + span, bottom + span
            chunks.append({"id": f"chunk_{cx}_{cy}", "x": cx, "y": cy,
                           "bounds_m": [left, bottom, right, top],
                           "tile_size": CHUNK_TILES,
                           "feature_counts": feature_layers})
    return {"chunk_tiles": CHUNK_TILES, "chunk_size_m": span, "chunks": chunks}


def generate_world(map_data: dict, output_dir: str | Path, seed: int = SEED) -> dict:
    """Generate pixel-art preview, control maps, atlases, manifest and ZIP."""
    validate_map(map_data)
    game_map = copy.deepcopy(map_data)
    residential_stats = generate_residential_properties(game_map, seed)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    bounds = _world_bounds(game_map)
    min_x, min_y, max_x, max_y = bounds
    width_m, height_m = max_x - min_x, max_y - min_y
    if width_m <= 0 or height_m <= 0:
        raise ValueError("A extensão geográfica do mapa é inválida.")
    ppm = min(PREVIEW_PIXELS_PER_METER, PREVIEW_MAX_EDGE / max(width_m, height_m))
    margin = 28
    width = int(math.ceil(width_m * ppm)) + margin * 2
    height = int(math.ceil(height_m * ppm)) + margin * 2
    if width * height > 11_000_000:
        raise ValueError("A prévia excede o limite seguro de pixels. Reduza o recorte do mapa.")

    def transform(point):
        return (int(round(margin + (float(point[0]) - min_x) * ppm)),
                int(round(margin + (max_y - float(point[1])) * ppm)))

    canvas = Image.new("RGB", (width, height), PALETTE["grass"])
    _draw_noise(canvas, seed, density=0.0016)
    draw = ImageDraw.Draw(canvas)

    # Blocks and plots create the underlying neighborhood structure.
    for block in game_map.get("blocks", []):
        zone = str(block.get("zone", "residential"))
        color = PALETTE["lot_mixed"] if zone == "mixed" else PALETTE["lot_commercial"] if zone == "commercial" else PALETTE["lot_residential"]
        for polygon in _rings_to_pixels(block.get("polygons"), transform):
            if polygon:
                draw.polygon(polygon[0], fill=color)
                draw.line(polygon[0] + [polygon[0][0]], fill=(80, 99, 68), width=1)
    for lot in game_map.get("lots", []):
        zone = str(lot.get("zone", "residential"))
        color = (104, 126, 82) if zone == "residential" else (126, 124, 91) if zone == "mixed" else (137, 121, 87)
        for polygon in _rings_to_pixels(lot.get("polygons"), transform):
            if polygon:
                draw.polygon(polygon[0], fill=color)
                draw.line(polygon[0] + [polygon[0][0]], fill=(157, 151, 115), width=1)

    # Roads: sidewalk bed, curb edge, asphalt and deterministic surface grain.
    road_draw_data = []
    for road in game_map.get("roads", []):
        road_id = road.get("id", "road")
        road_width = max(4.0, float(road.get("width_m") or 7.0)) * ppm
        kind = _road_kind(road)
        for source_line in road.get("lines", []):
            points = [transform(point) for point in source_line]
            if len(points) < 2:
                continue
            sidewalk_width = road_width + max(3.0, 3.0 * ppm)
            draw.line(points, fill=PALETTE["curb"], width=max(1, int(sidewalk_width + 3)), joint="curve")
            draw.line(points, fill=PALETTE["sidewalk"], width=max(1, int(sidewalk_width)), joint="curve")
            draw.line(points, fill=PALETTE["road_edge"], width=max(1, int(road_width + 2)), joint="curve")
            draw.line(points, fill=PALETTE["asphalt"], width=max(1, int(road_width)), joint="curve")
            _draw_road_texture(draw, points, road_width, _stable_int(f"{seed}:{road_id}"), kind == "arterial")
            if kind in {"arterial", "collector"} and road_width >= 12:
                _draw_lane_dashes(draw, points, road_width)
            road_draw_data.append((kind, points, road_width, road_id))

    # Crosswalks at major-road endpoints; this keeps markings tied to real OSM geometry.
    for kind, points, road_width, road_id in road_draw_data:
        if kind not in {"arterial", "collector"} or len(points) < 2:
            continue
        for endpoint, neighbor in ((points[0], points[1]), (points[-1], points[-2])):
            dx, dy = endpoint[0] - neighbor[0], endpoint[1] - neighbor[1]
            length = math.hypot(dx, dy) or 1
            ux, uy = dx / length, dy / length
            cx, cy = endpoint[0] - ux * max(5, road_width * 0.55), endpoint[1] - uy * max(5, road_width * 0.55)
            nx, ny = -uy, ux
            stripe_count = max(3, int(road_width / 3))
            for i in range(stripe_count):
                offset = (i - (stripe_count - 1) / 2) * max(3, road_width / stripe_count)
                p1 = (int(cx + nx * offset - ux * 2), int(cy + ny * offset - uy * 2))
                p2 = (int(cx + nx * offset + ux * 2), int(cy + ny * offset + uy * 2))
                draw.line((p1, p2), fill=PALETTE["marking"], width=2)

    # Buildings use footprints as masks, with procedural roof palettes and texture.
    styles = []
    for building in game_map.get("buildings", []):
        building_id = building.get("id", "building")
        building_seed = _stable_int(f"{seed}:{building_id}")
        kind = str(building.get("building_type", "house")).lower()
        category = "industrial" if any(t in kind for t in ("industrial", "warehouse", "factory")) else "commercial" if any(t in kind for t in ("commercial", "retail", "shop", "office")) else "residential"
        visual_style = building.get("visual_style") or {}
        style_entry = {
            "id": building_id,
            "category": category,
            "variant": visual_style.get("variant", building_seed % 4),
            "seed": visual_style.get("seed", building_seed),
        }
        if visual_style.get("residence_type"):
            style_entry["residence_type"] = visual_style["residence_type"]
        styles.append(style_entry)
        for polygon in _rings_to_pixels(building.get("polygons"), transform):
            _draw_roof(draw, polygon, building, building_seed)

    # Residential lot surfaces, driveways, walls and gates.
    for lot in game_map.get("lots", []):
        residence = lot.get("residential") or {}
        yard = residence.get("yard") or {}
        yard_palette = {
            "grass": (83, 112, 65),
            "garden": (74, 116, 65),
            "dirt": (132, 108, 76),
        }
        yard_fill = yard_palette.get(yard.get("surface"), yard_palette["grass"])
        for yard_poly in _rings_to_pixels(yard.get("polygons", []), transform):
            if not yard_poly:
                continue
            mask = Image.new("L", canvas.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.polygon(yard_poly[0], fill=255)
            for hole in yard_poly[1:]:
                mask_draw.polygon(hole, fill=0)
            canvas.paste(yard_fill, (0, 0), mask)

            outer = yard_poly[0]
            xs = [p[0] for p in outer]
            ys = [p[1] for p in outer]
            spacing = max(4, int(3.0 * ppm))
            for py in range(min(ys) + 2, max(ys), spacing):
                for px in range(min(xs) + 2, max(xs), spacing):
                    if not _point_in_ring((px, py), outer):
                        continue
                    if any(_point_in_ring((px, py), hole) for hole in yard_poly[1:]):
                        continue
                    if yard.get("surface") == "garden":
                        draw.point((px, py), fill=(112, 145, 76))
                    elif yard.get("surface") == "dirt":
                        draw.point((px, py), fill=(155, 129, 91))
                    else:
                        draw.point((px, py), fill=(102, 132, 75))

        garage_type = residence.get("garage_type", "uncovered")
        for garage_poly in _rings_to_pixels(residence.get("garage", []), transform):
            if not garage_poly:
                continue
            if garage_type == "covered":
                draw.polygon(garage_poly[0], fill=(111, 76, 59), outline=(55, 48, 41))
                for yy in range(min(p[1] for p in garage_poly[0]) + 3,
                                max(p[1] for p in garage_poly[0]), 5):
                    draw.line((min(p[0] for p in garage_poly[0]) + 2, yy,
                               max(p[0] for p in garage_poly[0]) - 2, yy),
                              fill=(78, 57, 47), width=1)
            else:
                draw.polygon(garage_poly[0], fill=(132, 132, 119), outline=(75, 79, 70))
                for yy in range(min(p[1] for p in garage_poly[0]) + 3,
                                max(p[1] for p in garage_poly[0]), 5):
                    draw.line((min(p[0] for p in garage_poly[0]) + 2, yy,
                               max(p[0] for p in garage_poly[0]) - 2, yy),
                              fill=(154, 153, 137), width=1)

        wall_type = residence.get("wall_type", "plaster")
        wall_colors = {
            "plaster": (194, 184, 156),
            "brick": (157, 111, 83),
            "concrete": (148, 153, 147),
        }
        wall_color = wall_colors.get(wall_type, wall_colors["plaster"])
        for wall in residence.get("walls", []):
            if len(wall) != 2:
                continue
            start, end = transform(wall[0]), transform(wall[1])
            draw.line((start, end), fill=(48, 54, 45), width=max(2, int(1.1 * ppm)))
            draw.line((start, end), fill=wall_color, width=max(1, int(0.65 * ppm)))
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy)
            if length < 4:
                continue
            nx, ny = -dy / length, dx / length
            spacing = max(5, int((2.0 if wall_type == "brick" else 3.5) * ppm))
            for offset in range(spacing, int(length), spacing):
                t = offset / length
                px, py = start[0] + dx * t, start[1] + dy * t
                joint = max(1, int(0.7 * ppm))
                draw.line((int(px - nx * joint), int(py - ny * joint),
                           int(px + nx * joint), int(py + ny * joint)),
                          fill=(91, 83, 68) if wall_type == "brick" else (224, 215, 190),
                          width=1)

        for gate in residence.get("gates", []):
            line = gate.get("line", [])
            if len(line) != 2:
                continue
            start, end = transform(line[0]), transform(line[1])
            draw.line((start, end), fill=(48, 54, 52), width=max(2, int(1.1 * ppm)))
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy) or 1.0
            bars = max(2, int(length / max(3.0, ppm * 0.7)))
            for index in range(1, bars):
                t = index / bars
                px, py = start[0] + dx * t, start[1] + dy * t
                draw.point((int(px), int(py)), fill=(202, 194, 163))

    # Existing OSM trees + repeatable trees in unoccupied lots.
    generated_trees = _generate_empty_lot_trees(game_map, seed)
    for index, tree in enumerate(game_map.get("trees", [])):
        x, y = transform(tree["point"])
        _draw_tree(draw, x, y, _stable_int(f"{seed}:osm:{tree.get('id', index)}"), ppm / 2)
    for tree in generated_trees:
        x, y = transform(tree["point"])
        _draw_tree(draw, x, y, _stable_int(f"{seed}:{tree['id']}"), ppm / 2)

    # Pixel-art frame and subtle coordinate-safe border.
    draw.rectangle((margin - 1, margin - 1, width - margin, height - margin), outline=(32, 47, 36), width=2)
    preview_path = output / "aracatexas_preview.png"
    canvas.save(preview_path, optimize=True)

    tileset = _make_tileset(output / "aracatexas_tileset.png", seed)
    building_atlas = _make_building_atlas(output / "aracatexas_buildings.png", seed)
    controls = _control_images(game_map, output, bounds)

    game_map["trees"] = list(game_map.get("trees", [])) + generated_trees
    style_by_id = {str(s["id"]): s for s in styles}
    for feature in game_map.get("buildings", []):
        style = style_by_id.get(str(feature.get("id")))
        if style:
            feature["visual_style"] = {
                k: style[k] for k in ("category", "variant", "seed", "residence_type")
                if k in style
            }
    game_map["visual"] = {
        "format": "aracatexas-world-v2",
        "tile_size_px": TILE_SIZE,
        "tile_size_m": TILE_METERS,
        "pixels_per_meter_in_game": TILE_SIZE / TILE_METERS,
        "seed": seed,
        "assets": {"tileset": "aracatexas_tileset.png", "buildings": "aracatexas_buildings.png", "preview": "aracatexas_preview.png"},
        "control_maps": controls,
        "generated_tree_count": len(generated_trees),
        "generated_residence_count": residential_stats["generated_houses"],
        "residential_property_count": residential_stats["properties"],
        "residential_generation": residential_stats,
        "building_styles": styles,
    }
    game_map_path = output / "aracatexas_map.json"
    game_map_path.write_text(json.dumps(game_map, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    counts = {
        "blocks": len(game_map.get("blocks", [])),
        "lots": len(game_map.get("lots", [])),
        "buildings": len(game_map.get("buildings", [])),
        "source_buildings": len(map_data.get("buildings", [])),
        "generated_residences": residential_stats["generated_houses"],
        "residential_properties": residential_stats["properties"],
        "roads": len(game_map.get("roads", [])),
        "osm_trees": len(map_data.get("trees", [])),
        "generated_trees": len(generated_trees),
    }
    chunk_index = _chunk_index(bounds, counts)
    (output / "chunk_index.json").write_text(json.dumps(chunk_index, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "name": "Araçatexas visual world",
        "format": "aracatexas-world-v2",
        "seed": seed,
        "crs": "EPSG:32722",
        "bounds_m": [round(v, 3) for v in bounds],
        "preview": {"file": preview_path.name, "width": width, "height": height, "pixels_per_meter": ppm},
        "tile_size_px": TILE_SIZE,
        "tile_size_m": TILE_METERS,
        "chunk_tiles": CHUNK_TILES,
        "counts": counts,
        "control_maps": controls,
        "tileset": tileset,
        "building_atlas": building_atlas,
        "game_map": game_map_path.name,
        "attribution": map_data.get("meta", {}).get("attribution", "© OpenStreetMap contributors · ODbL"),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "README.txt").write_text(_readme(manifest), encoding="utf-8")

    zip_path = output / "aracatexas_visual_world.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file() and path != zip_path:
                archive.write(path, path.relative_to(output))
    return {"output_dir": str(output), "preview": preview_path.name, "zip": zip_path.name,
            "game_map": game_map_path.name, "manifest": manifest, "counts": counts}


def _draw_lane_dashes(draw, points, road_width):
    dash_length, gap = 18.0, 17.0
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1:
            continue
        ux, uy = dx / length, dy / length
        pos = 3.0
        while pos < length - 2:
            end = min(length - 2, pos + dash_length)
            draw.line((int(a[0] + ux * pos), int(a[1] + uy * pos),
                       int(a[0] + ux * end), int(a[1] + uy * end)),
                      fill=PALETTE["marking"], width=max(1, int(road_width * 0.035)))
            pos += dash_length + gap


def _readme(manifest: dict) -> str:
    counts = manifest["counts"]
    return f"""ARAÇATEXAS WORLD BUILDER — VISUAL WORLD EXPORT

Formato: {manifest['format']}
Projeção: {manifest['crs']}
Tiles: {manifest['tile_size_px']}x{manifest['tile_size_px']} px = {manifest['tile_size_m']} m
Chunk: {manifest['chunk_tiles']} tiles
Seed: {manifest['seed']}

Conteúdo:
- {manifest['preview']['file']}: prévia renderizada em pixel art.
- {manifest['game_map']}: mapa compatível com o Araçatexas Game, com novas casas procedurais, propriedades muradas, portões, garagens, quintais e vegetação em lotes livres.
- {manifest['tileset']['file']}: atlas de chão, asfalto, calçadas e sinalização.
- {manifest['building_atlas']['file']}: atlas inicial de fachadas/volumes modulares.
- control_*.png: imagens de controle em grade de 2 m por pixel.
- chunk_index.json: índice espacial para carregamento regional futuro.
- manifest.json: metadados e catálogo de recursos.

Feições: {counts['blocks']} quadras, {counts['lots']} lotes, {counts['buildings']} construções ({counts['generated_residences']} novas residências) e {counts['roads']} vias.
Propriedades residenciais estruturadas: {counts['residential_properties']}.
Árvores OSM: {counts['osm_trees']}; árvores adicionadas em lotes livres: {counts['generated_trees']}.

INTEGRAÇÃO COM O JOGO
1. Faça uma cópia de segurança de public/data/aracatexas_map.json.
2. Substitua esse arquivo pelo JSON exportado ({manifest['game_map']}).
3. Preserve os atlas atuais do jogo; as casas continuam usando a textura existente.
4. O mapa base mantém o formato de coordenadas local em metros (EPSG:32722).
5. Consulte visual.* dentro do JSON para os caminhos de atlas, escala e estilos.

A prévia é um raster de inspeção. O jogo continua desenhando casas, muros, portões e garagens como geometrias separadas em Phaser para preservar colisões e futuras interações.

Dados geográficos: {manifest['attribution']}.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera a primeira camada visual pixel-art do Araçatexas.")
    parser.add_argument("--source", type=Path, required=True, help="Caminho para public/data/aracatexas_map.json do Araçatexas Game.")
    parser.add_argument("--output", type=Path, default=Path("output/aracatexas_visual_world"))
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    map_data = json.loads(args.source.read_text(encoding="utf-8"))
    result = generate_world(map_data, args.output, args.seed)
    print(json.dumps({"output": result["output_dir"], "counts": result["counts"], "zip": result["zip"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
