# NextDrive - Deluge 2.x plugin
# Copyright (C) 2026 jigglenilly
# SPDX-License-Identifier: GPL-3.0-or-later
from setuptools import find_packages, setup

__plugin_name__ = 'NextDrive'
__author__ = 'jigglenilly'
__author_email__ = ''
__version__ = '1.0.0'
__url__ = 'https://github.com/jigglenilly/deluge-nextdrive'
__license__ = 'GPL-3.0-or-later'
__description__ = 'Sends each new torrent to the first download location with room, before any data is written.'
__long_description__ = __description__
__pkg_data__ = {'deluge_' + __plugin_name__.lower(): ['data/*']}

setup(
    name=__plugin_name__,
    version=__version__,
    description=__description__,
    author=__author__,
    author_email=__author_email__,
    url=__url__,
    license=__license__,
    long_description=__long_description__,
    packages=find_packages(exclude=['tests']),
    package_data=__pkg_data__,
    entry_points="""
    [deluge.plugin.core]
    %s = deluge_%s:CorePlugin
    [deluge.plugin.web]
    %s = deluge_%s:WebUIPlugin
    """ % ((__plugin_name__, __plugin_name__.lower()) * 2),
)
