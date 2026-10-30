deluge-nextdrive
================
https://github.com/jigglenilly/deluge-nextdrive


Description
-----------

*Deluge plugin that picks the first download drive with room for a new torrent.*

You give NextDrive an ordered list of download folders. When a torrent is added, the plugin
measures the files you selected, checks free space on each folder in turn and sends the torrent
to the first one that can hold it while still leaving a reserve free (50 GiB by default).
The location is set before any data is written, so nothing ever has to be moved.

Features:

* Ordered fallback list, editable from the Web UI (add, remove, up, down)
* Reserve per drive, or one global default
* Space still owed to unfinished downloads on the same drive is subtracted
* Magnets are held at zero bytes until metadata arrives, then placed
* Optional label per drive (applied only to unlabeled torrents)
* Respects Label plugin labels that have their own move-completed path
* If nothing fits, the torrent stays on the default folder with a warning, optionally paused

Deluge is a lightweight, Free Software, cross-platform BitTorrent client. Download it at https://deluge-torrent.org/

.. note:: Built and tested for Deluge 2.2.x on Python 3.12 (libtorrent 2.0.x).
.. note:: The egg's Python version must match the Python running Deluge.


Installation
------------

1. Get plugin egg file from the ``dist/`` directory.

2. Open Deluge Web UI, go to "Preferences -> Plugins -> Install plugin" and choose egg file.
   Alternatively, copy the egg into the ``plugins`` folder of your Deluge config directory and restart Deluge once.

3. Activate ``NextDrive`` plugin.

4. Open "Preferences -> NextDrive", add your download folders in order and press "Apply".

5. In case you use Client-Server setup, the plugin must be installed on the server (daemon) side as well,
   see - https://dev.deluge-torrent.org/wiki/Plugins#Client-ServerSetups


.. note::

    To build .egg file from source code yourself use ``./build.sh`` (or ``python setup.py bdist_egg``)
    in source code directory, using the same Python version as your Deluge.


Configuration
-------------

Settings live in ``nextdrive.conf`` in the Deluge config directory. The Web UI page is the easiest
way to change them; ``nextdrive.conf.example`` shows every key.

* ``locations`` - ordered list of ``{path, reserve_gb, label}``. Empty ``reserve_gb`` means default.
* ``reserve_gb`` - default free space to keep on each drive, in GiB (``50``).
* ``only_default_location`` - only reroute torrents headed to the default download folder (``true``).
* ``handle_magnets`` - hold magnets until metadata arrives, then decide (``true``).
* ``count_committed`` - subtract space still owed to unfinished downloads (``true``).
* ``override_label_paths`` - reroute even when the torrent's label has its own path (``false``).
* ``pause_if_no_fit`` - pause the torrent when no drive has room (``false``).
* ``label_delay`` - seconds to wait before applying a drive label (``10``).

Every decision is written to the Deluge log with the ``NextDrive:`` prefix.


Safety
------

* Existing torrents are never touched, and a torrent that already has data is never moved.
* Torrents loaded from saved state, added in seed mode, or whose data already exists at the
  requested path are left alone.
* A torrent added with an explicit, non-default path is left alone (unless you disable
  ``only_default_location``).
* Free space is checked at add time only. Several large torrents added at once are accounted
  for, but other programs writing to the same drive are not.
* Tools that set a download path through a label (for example \*arr apps using a label with a
  move-completed path) keep working, because those labels are not overridden by default.
* Disabling the plugin restores Deluge's original add behaviour immediately.


License
-------

GPLv3 or later, see ``LICENSE``. Deluge itself is GPLv3 and this plugin imports and runs inside it.
