from pathlib import Path

import pytest

from bridgebox.config import Config, load_config, save_config


def test_defaults_when_no_file(tmp_path: Path):
    config = load_config(tmp_path / "does-not-exist.yaml")

    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8443
    assert config.zapret.enabled is True
    assert config.zapret.dir == "zapret"
    assert config.zapret.strategy == "general"
    assert config.logging.level == "info"
    assert config.ui.theme == "dark"
    assert config.ui.animations_enabled is True


def test_yaml_overrides_defaults(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
server:
  port: 9000
zapret:
  strategy: general-alt11
ui:
  theme: dark
  animations_enabled: false
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.server.port == 9000
    assert config.server.host == "127.0.0.1"  # untouched default
    assert config.zapret.strategy == "general-alt11"
    assert config.ui.theme == "dark"
    assert config.ui.animations_enabled is False


def test_invalid_port_raises(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 70000\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)


def test_invalid_log_level_raises(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("logging:\n  level: verbose\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_returns_config_instance(tmp_path: Path):
    config = load_config(tmp_path / "missing.yaml")
    assert isinstance(config, Config)


def test_save_config_then_load_round_trips(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    original = Config()
    original.server.port = 9999
    original.zapret.strategy = "alternative-11"
    original.ui.theme = "dark"

    save_config(original, config_path)
    loaded = load_config(config_path)

    assert loaded == original


def test_save_config_creates_human_readable_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"

    save_config(Config(), config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "server:" in text
    assert "port: 8443" in text


def test_zapret_dir_rejects_paths_outside_the_install():
    """zapret.dir is where a .bat gets executed from by a process that must
    run as Administrator. BridgeBox ships portable, so config.yaml lives in a
    user-writable folder - one edited line must not become privilege
    escalation."""
    import pytest
    from bridgebox.config import ZapretConfig

    for escape in ("C:/Users/Public/evil", "../..", "../../Users/Public"):
        with pytest.raises(ValueError):
            ZapretConfig(dir=escape)


def test_zapret_dir_still_accepts_the_normal_relative_value():
    from bridgebox.config import ZapretConfig

    assert ZapretConfig(dir="zapret").dir == "zapret"
    assert ZapretConfig().dir == "zapret"


def test_proxy_paths_are_normalised_and_validated():
    import pytest
    from bridgebox.config import ProxyConfig

    # Trailing slashes dropped and blanks removed, so matching is one rule.
    assert ProxyConfig(paths=["/api/", " /tts ", ""]).paths == ["/api", "/tts"]
    # "/" survives as the match-everything prefix.
    assert ProxyConfig(paths=["/"]).paths == ["/"]

    for bad in (["api"], ["/two words"], []):
        with pytest.raises(ValueError):
            ProxyConfig(paths=bad)


def test_proxy_forwards_everything_by_default():
    """Narrowing this is opt-in: an unlisted path is an endpoint nobody had
    seen yet, which is how FixyText's /tts/generate broke."""
    from bridgebox.config import Config, ProxyConfig

    assert ProxyConfig().forward_all is True
    assert Config().proxy.paths == ["/api", "/tts", "/media"]


def test_a_null_patch_resets_the_proxy_section():
    from bridgebox.config import Config, ProxyConfig
    from bridgebox.desktop import _deep_merge

    merged = Config(proxy=ProxyConfig(forward_all=False, paths=["/only"])).model_dump()
    _deep_merge(merged, {"proxy": None})

    assert Config.model_validate(merged).proxy == ProxyConfig()
