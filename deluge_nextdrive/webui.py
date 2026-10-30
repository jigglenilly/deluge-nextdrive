# Copyright (C) 2026 jigglenilly
# SPDX-License-Identifier: GPL-3.0-or-later
import logging

from deluge.plugins.pluginbase import WebPluginBase

from .common import get_resource

log = logging.getLogger(__name__)


class WebUI(WebPluginBase):
    scripts = [get_resource('nextdrive.js')]
    debug_scripts = scripts
