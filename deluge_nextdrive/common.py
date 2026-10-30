# Copyright (C) 2026 jigglenilly
# SPDX-License-Identifier: GPL-3.0-or-later
import os


def get_resource(filename):
    """Return a filesystem path for a file in the plugin's data folder.

    pkg_resources is tried first because it extracts files from a zipped
    .egg to a persistent cache, which is how Deluge 2.x loads plugins.
    """
    path = os.path.join('data', filename)
    try:
        from pkg_resources import resource_filename

        return resource_filename(__package__, path)
    except ImportError:
        from deluge.common import resource_filename

        return resource_filename(__package__, path)
