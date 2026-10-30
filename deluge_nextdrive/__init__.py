# NextDrive - pick the first download location with room, before data is written.
# Copyright (C) 2026 jigglenilly
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deluge plugin entry points for NextDrive."""

from deluge.plugins.init import PluginInitBase


class CorePlugin(PluginInitBase):
    def __init__(self, plugin_name):
        from .core import Core as _plugin_cls

        self._plugin_cls = _plugin_cls
        super().__init__(plugin_name)


class WebUIPlugin(PluginInitBase):
    def __init__(self, plugin_name):
        from .webui import WebUI as _plugin_cls

        self._plugin_cls = _plugin_cls
        super().__init__(plugin_name)
