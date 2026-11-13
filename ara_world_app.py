"""Standalone entry point for the Araçatexas visual-world tool.

Run with: python ara_world_app.py
Then open http://127.0.0.1:5001/.
The existing Knoxify app remains available through python app.py.
"""
from flask import Flask
from generator.ara_world_web import ara_world_bp

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.register_blueprint(ara_world_bp)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
