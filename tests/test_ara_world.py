import hashlib
import json
import tempfile
import unittest
import random

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from generator.residential import _frontage_edge, _wall_and_gate_layout
from pathlib import Path

from PIL import Image

from generator.ara_world import generate_world, validate_map


SAMPLE_MAP = {
    "meta": {"crs": "EPSG:32722", "radius_m": 80, "origin_lonlat": [-50.43568, -21.22224]},
    "blocks": [{"id": "block_a", "zone": "residential", "polygons": [[[
        [-34, -30], [34, -30], [34, 30], [-34, 30], [-34, -30]
    ]]]}],
    "lots": [
        {"id": "lot_house", "zone": "residential", "polygons": [[[
            [-25, -18], [-2, -18], [-2, 18], [-25, 18], [-25, -18]
        ]]]},
        {"id": "lot_empty", "zone": "residential", "polygons": [[[
            [3, -18], [25, -18], [25, 18], [3, 18], [3, -18]
        ]]]},
    ],
    "buildings": [{"id": "house_a", "lot_id": "lot_house", "building_type": "house", "polygons": [[[
        [-20, -9], [-7, -9], [-7, 9], [-20, 9], [-20, -9]
    ]]]}],
    "roads": [{"id": 1, "name": "Avenida Teste", "highway": "primary", "width_m": 14,
               "lines": [[[-80, 0], [80, 0]]]}],
    "trees": [{"id": 12, "point": [28, 20]}],
}


class AraWorldTests(unittest.TestCase):
    def test_rejects_wrong_crs_or_missing_roads(self):
        bad = dict(SAMPLE_MAP)
        bad["meta"] = {"crs": "EPSG:4326"}
        with self.assertRaises(ValueError):
            validate_map(bad)
        bad = dict(SAMPLE_MAP)
        bad["roads"] = []
        with self.assertRaises(ValueError):
            validate_map(bad)

    def test_generates_game_map_controls_atlases_and_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            result = generate_world(SAMPLE_MAP, out)
            expected = [
                "aracatexas_preview.png", "aracatexas_tileset.png",
                "aracatexas_buildings.png", "aracatexas_map.json",
                "control_ground.png", "control_zones.png", "control_roads.png",
                "control_buildings.png", "chunk_index.json", "manifest.json",
                "README.txt", "aracatexas_visual_world.zip",
            ]
            for filename in expected:
                self.assertTrue((out / filename).is_file(), filename)
            with Image.open(out / "aracatexas_preview.png") as preview:
                self.assertGreater(preview.width, 100)
                self.assertGreater(preview.height, 100)
            with Image.open(out / "aracatexas_tileset.png") as tiles:
                self.assertEqual(tiles.size, (256, 128))
            with Image.open(out / "aracatexas_buildings.png") as buildings:
                self.assertEqual(buildings.size, (256, 128))
            game_map = json.loads((out / "aracatexas_map.json").read_text(encoding="utf-8"))
            self.assertEqual(game_map["meta"]["crs"], "EPSG:32722")
            self.assertEqual(game_map["visual"]["tile_size_px"], 32)
            self.assertGreaterEqual(len(game_map["trees"]), len(SAMPLE_MAP["trees"]))
            self.assertEqual(result["counts"]["roads"], 1)
            generated = [item for item in game_map["buildings"]
                         if item.get("source") == "procedural-residential-lot"]
            self.assertGreaterEqual(len(generated), 1)
            self.assertEqual(game_map["visual"]["generated_residence_count"], len(generated))
            empty_lot = next(item for item in game_map["lots"] if item["id"] == "lot_empty")
            self.assertTrue(empty_lot["residential"]["walls"])
            self.assertTrue(empty_lot["residential"]["gates"])
            self.assertIn(empty_lot["residential"]["yard"]["class"], {"small", "medium", "large"})
            self.assertTrue(empty_lot["residential"]["yard"]["polygons"])
            self.assertIn(empty_lot["residential"]["yard"]["surface"], {"grass", "garden", "dirt"})
            self.assertIn(empty_lot["residential"]["layout"], {
                "front_house_back_yard", "rear_house_front_yard",
                "center_house_front_back", "large_house", "compact_house"
            })
            self.assertIn(empty_lot["residential"]["garage_type"], {"covered", "uncovered", "none"})
            self.assertIn(empty_lot["residential"]["wall_type"], {"plaster", "brick", "concrete"})
            self.assertGreater(empty_lot["residential"]["house"]["length_m"], 0)
            self.assertIn("rear", empty_lot["residential"]["setbacks_m"])
            self.assertGreaterEqual(empty_lot["residential"]["garage_area_m2"], 0)
            generated_house = next(item for item in generated if item["id"] in
                                   empty_lot["residential"]["house"]["building_ids"])
            self.assertIn(generated_house["visual_style"]["residence_type"],
                          {"casa_terrea", "casa_antiga", "sobrado", "casa_estendida"})
            self.assertEqual(len(generated), 1)
            self.assertTrue((out / "aracatexas_visual_world.zip").stat().st_size > 1000)

    def test_regenerating_an_export_does_not_duplicate_or_lose_house_type(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            generate_world(SAMPLE_MAP, first, seed=19)
            first_map = json.loads((first / "aracatexas_map.json").read_text(encoding="utf-8"))
            first_lot = next(item for item in first_map["lots"] if item["id"] == "lot_empty")
            original_type = first_lot["residential"]["type"]

            result = generate_world(first_map, second, seed=19)
            second_map = json.loads((second / "aracatexas_map.json").read_text(encoding="utf-8"))
            generated = [item for item in second_map["buildings"]
                         if item.get("source") == "procedural-residential-lot"]
            second_lot = next(item for item in second_map["lots"] if item["id"] == "lot_empty")
            self.assertEqual(len(generated), 1)
            self.assertEqual(result["counts"]["generated_residences"], 0)
            self.assertTrue(second_lot["residential"]["generated"])
            self.assertEqual(second_lot["residential"]["type"], original_type)
            self.assertEqual(second_lot["residential"]["layout"], first_lot["residential"]["layout"])

    def test_frontage_prefers_road_parallel_edge_over_crossing_side(self):
        parcel = Polygon([(0, -10), (20, -10), (20, 10), (0, 10), (0, -10)])
        road = LineString([(-10, 0), (30, 0)])
        frontage = _frontage_edge(parcel, unary_union([road]), [(road, 7.0)])
        self.assertIsNotNone(frontage)
        self.assertAlmostEqual(frontage.coords[0][1], frontage.coords[-1][1], places=6)
        self.assertGreater(abs(frontage.coords[0][1]), 9.0)

    def test_wall_generation_keeps_side_edges_that_only_touch_frontage_corners(self):
        parcel = Polygon([(0, 0), (20, 0), (20, 15), (0, 15), (0, 0)])
        frontage = LineString([(0, 0), (20, 0)])
        walls, _, _, _, _ = _wall_and_gate_layout(
            parcel, frontage, random.Random(5), None
        )
        keys = {
            frozenset((tuple(round(v, 2) for v in wall[0]),
                       tuple(round(v, 2) for v in wall[1])))
            for wall in walls
        }
        self.assertIn(frozenset({(0.0, 0.0), (0.0, 15.0)}), keys)
        self.assertIn(frozenset({(20.0, 0.0), (20.0, 15.0)}), keys)
        self.assertIn(frozenset({(0.0, 15.0), (20.0, 15.0)}), keys)

    def test_does_not_generate_houses_in_commercial_lots(self):
        source = json.loads(json.dumps(SAMPLE_MAP))
        source["lots"][1]["zone"] = "commercial"
        with tempfile.TemporaryDirectory() as temp:
            result = generate_world(source, Path(temp), seed=19)
            game_map = json.loads((Path(temp) / "aracatexas_map.json").read_text(encoding="utf-8"))
            generated = [item for item in game_map["buildings"]
                         if item.get("source") == "procedural-residential-lot"]
            self.assertEqual(generated, [])
            self.assertEqual(result["counts"]["generated_residences"], 0)

    def test_same_seed_produces_same_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "a", Path(temp) / "b"
            generate_world(SAMPLE_MAP, first, seed=77)
            generate_world(SAMPLE_MAP, second, seed=77)
            hash_a = hashlib.sha256((first / "aracatexas_preview.png").read_bytes()).hexdigest()
            hash_b = hashlib.sha256((second / "aracatexas_preview.png").read_bytes()).hexdigest()
            self.assertEqual(hash_a, hash_b)
            map_a = json.loads((first / "aracatexas_map.json").read_text(encoding="utf-8"))
            map_b = json.loads((second / "aracatexas_map.json").read_text(encoding="utf-8"))
            self.assertEqual(map_a["buildings"], map_b["buildings"])
            self.assertEqual(map_a["lots"], map_b["lots"])


if __name__ == "__main__":
    unittest.main()
