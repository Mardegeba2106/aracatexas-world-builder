"""Flask blueprint for the Araçatexas visual-world tool."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from generator.ara_world import generate_world

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "output" / "ara_world_runs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ara_world_bp = Blueprint("ara_world", __name__)


@ara_world_bp.get("/")
def page():
    return render_template("ara_world.html")


@ara_world_bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "Araçatexas Visual World Builder"})


@ara_world_bp.post("/api/generate")
def api_generate():
    uploaded = request.files.get("map")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Selecione o map.json do Araçatexas Game."}), 400
    if Path(secure_filename(uploaded.filename)).suffix.lower() != ".json":
        return jsonify({"error": "O arquivo enviado precisa ser .json."}), 400
    try:
        map_data = json.load(uploaded.stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return jsonify({"error": f"Não foi possível ler o JSON: {exc}"}), 400
    run_id = uuid.uuid4().hex[:12]
    run_dir = OUTPUT_DIR / run_id
    try:
        result = generate_world(map_data, run_dir)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400

    def file_url(filename: str) -> str:
        return url_for("ara_world.download_run_file", run_id=run_id, filename=filename)

    return jsonify({
        "ok": True,
        "runId": run_id,
        "counts": result["counts"],
        "preview": file_url(result["preview"]),
        "zip": file_url(result["zip"]),
        "gameMap": file_url(result["game_map"]),
        "manifest": file_url("manifest.json"),
        "tileset": file_url("aracatexas_tileset.png"),
        "buildings": file_url("aracatexas_buildings.png"),
    })


@ara_world_bp.get("/runs/<run_id>/<filename>")
def download_run_file(run_id: str, filename: str):
    if not run_id.isalnum() or not filename or Path(filename).name != filename:
        return jsonify({"error": "Arquivo não encontrado."}), 404
    run_dir = (OUTPUT_DIR / run_id).resolve()
    if run_dir.parent != OUTPUT_DIR.resolve() or not run_dir.is_dir():
        return jsonify({"error": "Exportação não encontrada."}), 404
    return send_from_directory(run_dir, filename,
                               as_attachment=filename.endswith((".zip", ".json")))


@ara_world_bp.app_errorhandler(413)
def too_large(_error):
    return jsonify({"error": "O arquivo ultrapassa o limite de 20 MB."}), 413
