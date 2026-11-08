from pathlib import Path

import pytest

from clank_server.config import Settings


def test_config_file_env_and_flags(tmp_path, monkeypatch):
    cfg = tmp_path / "clank.toml"
    cfg.write_text(
        'model = "from-file"\nllm_url = "http://file"\nschedule = ["fresh"]\n'
        'lean_project = "proj"\ncache_path = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLANK_CONFIG", str(cfg))
    monkeypatch.setenv("CLANK_LLM_URL", "http://env")
    monkeypatch.delenv("CLANK_MODEL", raising=False)
    s, _ = Settings.load([])
    assert s.model == "from-file"  # file overrides defaults
    assert s.llm_url == "http://env"  # environment overrides the file
    assert s.schedule == ("fresh",)
    assert s.lean_project == (tmp_path / "proj").resolve()  # relative to the file
    assert s.cache_path is None  # "" disables


def test_config_flag_and_home_path(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('cache_path = "~/x.sqlite"\n', encoding="utf-8")
    monkeypatch.delenv("CLANK_CONFIG", raising=False)
    s, _ = Settings.load(["--config", str(cfg), "--port", "1"])
    assert s.cache_path == (Path.home() / "x.sqlite").resolve()


def test_config_rejects_unknown_keys(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("modle = 'typo'\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="unknown settings"):
        Settings.from_file(cfg)
