"""config.yaml is a user-writable, 'human-editable' file. load_config() is
DELIBERATELY strict about it - a bad port or log level should raise, not
silently coerce (see test_config.py's test_invalid_port_raises et al, which
this file does not change or contradict) - so every test below still drives
bridgebox.config.load_config directly with the exact bytes a user could put
on disk and confirms it still raises for a non-mapping file, a YAML syntax
error, a wrong-typed section, or a rejected security value (zapret.dir
escaping its sandbox).

What WAS a real defect, now fixed in bridgebox/desktop.py's main(): that
raise used to propagate straight out of load_config's only caller with no
try/except around it (migrate_config_file, right below it, WAS already
guarded) - so under the frozen windowed/pythonw build, with no console, the
app just died silently and the user was locked out with no way back to
Settings to fix it. main() now catches it and falls back to Config()
defaults; see test_desktop.py's
test_main_falls_back_to_defaults_when_config_yaml_is_broken for that half.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bridgebox.config import load_config, migrate_config_file


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "body",
    [
        "7",                       # a bare scalar
        "just a string",           # a bare string
        "- a\n- b\n- c\n",         # a top-level sequence
        "[1, 2, 3]",               # a flow sequence
    ],
)
def test_non_mapping_yaml_crashes_load_config(tmp_path, body):
    """A config.yaml whose top level is anything but a mapping still raises
    out of load_config() itself - yaml.safe_load happily returns an int /
    list / str, `raw or {}` keeps it, and Config.model_validate(<non-dict>)
    raises. That is intentional and unchanged (see test_config.py); what
    used to be missing was anyone catching it - see this file's module
    docstring for where that fix actually lives."""
    cfg = _write(tmp_path, body)
    with pytest.raises(ValidationError):
        load_config(cfg)


def test_yaml_syntax_error_crashes_load_config(tmp_path):
    """A genuine YAML syntax error (trivially produced by a botched manual
    edit) still isn't caught inside load_config - by design, same as the
    non-mapping case above; desktop.main() is what now catches it."""
    cfg = _write(
        tmp_path,
        """\
        ui:
          theme: dark
            language: en
        """,
    )
    with pytest.raises(yaml.YAMLError):
        load_config(cfg)


def test_wrong_typed_section_crashes_load_config(tmp_path):
    """`server:` given a list instead of a mapping. Same class as above -
    load_config still raises ValidationError, on purpose."""
    cfg = _write(tmp_path, "server: []\n")
    with pytest.raises(ValidationError):
        load_config(cfg)


def test_security_validator_rejection_still_raises_out_of_load_config(tmp_path):
    """zapret.dir has a validator whose stated purpose is to stop a local
    user pointing code-execution at a directory they control (LPE) - feeding
    it a traversal value correctly raises ValidationError here, same as any
    other invalid field. What used to make this dangerous specifically is
    that the very rejection meant to stop an attack instead bricked startup
    with no way back into Settings to fix it - main() now catches this (and
    every other ValidationError/YAMLError from load_config) and falls back
    to defaults instead of dying.
    """
    cfg = _write(
        tmp_path,
        """\
        zapret:
          dir: ../../Windows
        """,
    )
    with pytest.raises(ValidationError):
        load_config(cfg)


def test_migrate_config_file_no_longer_crashes_on_non_mapping_yaml(tmp_path):
    """Regression: migrate_config_file used to throw the wrong exception
    type for a non-mapping file - dict(7) -> TypeError from
    _fill_missing_defaults - instead of treating a corrupt file as 'nothing
    to migrate'. It was already wrapped in try/except in main(), so this
    never bricked startup, but it did mean migration silently no-op'd via
    an unrelated exception type rather than a clean, intentional skip."""
    cfg = _write(tmp_path, "7\n")
    assert migrate_config_file(cfg) is False


def test_migrate_config_file_no_longer_crashes_on_top_level_sequence(tmp_path):
    cfg = _write(tmp_path, "- a\n- b\n")
    assert migrate_config_file(cfg) is False
