"""Focused contracts for terminal options in the default config."""

import os
from unittest.mock import patch

from hermes_cli.config import load_config


def test_missing_config_uses_default_terminal_font_size(tmp_path):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        assert load_config()["terminal"]["font_size"] == 11
