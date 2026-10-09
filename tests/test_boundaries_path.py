"""Boundary files resolve outside the container (CI runner, local checkout)."""
from src import boundaries


def test_configured_container_path_falls_back_to_repo_data(tmp_path):
    missing = "/app/data/NB.geojson"
    resolved = boundaries._resolve_layer_path(missing)
    assert resolved.exists(), resolved
    assert resolved.name == "NB.geojson"


def test_existing_configured_path_wins(tmp_path):
    f = tmp_path / "x.geojson"
    f.write_text("{}")
    assert boundaries._resolve_layer_path(str(f)) == f
