from pathlib import Path

import pytest

import yaml

from bridgebox.config import Config, load_config, migrate_config_file, save_config


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


# ---- migrate_config_file: safe merge of new default fields ---------------


def test_migrate_config_file_does_nothing_when_there_is_no_file(tmp_path: Path):
    """config.yaml is never created by this app on its own - see
    save_config's docstring. Migration must not become the first thing that
    creates one."""
    path = tmp_path / "config.yaml"

    assert migrate_config_file(path) is False
    assert not path.exists()


def test_migrate_config_file_adds_a_brand_new_top_level_section(tmp_path: Path):
    """A version bump that introduces a whole new config section (like
    app_update in this very change) must show up in an existing file, not
    just resolve silently in memory."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"server": {"port": 9000}}), encoding="utf-8")

    changed = migrate_config_file(path)

    assert changed is True
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk["server"]["port"] == 9000, "the user's own value must survive"
    assert "app_update" in on_disk
    assert on_disk["app_update"]["check_on_startup"] is True


def test_migrate_config_file_fills_a_missing_field_inside_an_existing_section(tmp_path: Path):
    """The common case: an old file has SOME keys of a section (ui.theme)
    but not one a later version added (ui.autostart)."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"ui": {"theme": "light"}}), encoding="utf-8")

    migrate_config_file(path)

    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk["ui"]["theme"] == "light", "existing value must not be touched"
    assert on_disk["ui"]["autostart"] is False  # filled in from the schema default


def test_migrate_config_file_never_touches_an_explicit_null_reset(tmp_path: Path):
    """None is this app's own "reset this section to defaults" signal (see
    _deep_merge) - migration must treat it as a value that is PRESENT, not
    as something missing to fill in, or it would silently cancel a user's
    reset the next time the app starts."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"proxy": None}), encoding="utf-8")

    migrate_config_file(path)

    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk["proxy"] is None


def test_migrate_config_file_leaves_an_up_to_date_file_untouched(tmp_path: Path):
    """No new fields to add -> no write at all, not even a reformat. A
    config.yaml a user hand-edited (comments, key order) must not get
    silently rewritten on every single launch."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(Config().model_dump(), sort_keys=False), encoding="utf-8")
    original_text = path.read_text(encoding="utf-8")

    changed = migrate_config_file(path)

    assert changed is False
    assert path.read_text(encoding="utf-8") == original_text


def test_migrate_config_file_result_still_loads_correctly(tmp_path: Path):
    """The point of all this: after migration, load_config sees the same
    effective config it would have seen anyway (defaults filled in), just
    now written out explicitly."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"zapret": {"strategy": "general"}}), encoding="utf-8")

    migrate_config_file(path)

    assert load_config(path) == Config()
