# NextDrive - pick the first download location with room, before data is written.
# Copyright (C) 2026 jigglenilly
# SPDX-License-Identifier: GPL-3.0-or-later
"""NextDrive core plugin.

When a new torrent is added, NextDrive looks at an ordered list of download
locations and sends the torrent to the first one that has room for it plus a
reserve. The choice is made before libtorrent writes any data:

* .torrent files: the download location in the add options is changed before
  the torrent is handed to libtorrent.
* magnet links: the torrent is held in libtorrent's ``upload_mode`` (it can
  fetch metadata but cannot download any file data, the same trick Deluge
  uses to prefetch magnet metadata). When the metadata arrives the decision
  is made while it still has 0 bytes, and then the hold is released.

NextDrive never moves a torrent that already has data and never touches
torrents that were already in the session.
"""

import logging
import os
import shutil

import deluge.component as component
import deluge.configmanager
from deluge._libtorrent import lt
from deluge.core.rpcserver import export
from deluge.plugins.pluginbase import CorePluginBase
from twisted.internet import reactor

log = logging.getLogger(__name__)

GIB = 1024**3

DEFAULT_PREFS = {
    # Master switch.
    'enabled': True,
    # Ordered list of {'path': str, 'reserve_gb': float or None, 'label': str}.
    # reserve_gb None/blank means "use the global reserve_gb".
    'locations': [],
    # Global free-space reserve in GiB kept on every location.
    'reserve_gb': 50.0,
    # Only reroute torrents that were going to Deluge's default download
    # location. A torrent added with an explicit, different path is left alone.
    'only_default_location': True,
    # If a torrent's Label (Label plugin) has its own move-completed path,
    # leave the torrent alone unless this is True.
    'override_label_paths': False,
    # If no location fits: leave the torrent on its requested path and log a
    # warning. With this on, it is also added/kept paused.
    'pause_if_no_fit': False,
    # Hold magnet links until metadata arrives, then decide.
    'handle_magnets': True,
    # Subtract the bytes still to be downloaded by unfinished torrents on
    # each drive from that drive's free space.
    'count_committed': True,
    # Seconds to wait before applying a per-location label, so a label set
    # by the adding client (e.g. Sonarr/Radarr) always wins.
    'label_delay': 10,
    # Internal: magnets waiting for metadata {torrent_id: {...}}.
    'pending_magnets': {},
}


def _norm(path):
    return os.path.normpath(os.path.expanduser(path or '')).rstrip(os.sep) or os.sep


def _h(num):
    """Human-readable size for the log."""
    if abs(num) >= GIB:
        return '%.2f GiB' % (num / GIB)
    return '%.1f MiB' % (num / 1024**2)


def _dev_of(path):
    """st_dev of path, or of its closest existing parent."""
    path = _norm(path)
    while True:
        try:
            return os.stat(path).st_dev
        except OSError:
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent


class Core(CorePluginBase):
    def enable(self):
        self.config = deluge.configmanager.ConfigManager(
            'nextdrive.conf', DEFAULT_PREFS
        )
        self.tm = component.get('TorrentManager')
        self.core_config = component.get('Core').config
        self._inflight = {}  # torrent_id -> (st_dev, bytes) chosen but not yet in tm
        self._holding = False
        self._wrapped = {}

        for name in ('add', 'add_async', '_build_torrent_params'):
            original = getattr(self.tm, name)
            had_attr = name in self.tm.__dict__
            wrapper = getattr(self, '_wrap_' + name)(original)
            self._wrapped[name] = (original, had_attr, wrapper)
            setattr(self.tm, name, wrapper)

        am = component.get('AlertManager')
        am.register_handler('metadata_received', self._on_metadata_received)
        em = component.get('EventManager')
        em.register_event_handler('TorrentRemovedEvent', self._on_torrent_removed)
        em.register_event_handler('SessionStartedEvent', self._rearm_pending)
        reactor.callLater(0, self._rearm_pending)
        log.info(
            'NextDrive enabled with %d location(s)', len(self.config['locations'])
        )

    def disable(self):
        for name, (original, had_attr, wrapper) in self._wrapped.items():
            if getattr(self.tm, name, None) is wrapper:
                if had_attr:
                    setattr(self.tm, name, original)
                else:
                    delattr(self.tm, name)
        self._wrapped = {}
        component.get('AlertManager').deregister_handler(self._on_metadata_received)
        em = component.get('EventManager')
        em.deregister_event_handler('TorrentRemovedEvent', self._on_torrent_removed)
        em.deregister_event_handler('SessionStartedEvent', self._rearm_pending)
        # Never leave a magnet stuck on hold when the plugin goes away.
        for torrent_id, info in list(self.config['pending_magnets'].items()):
            self._release(torrent_id, info)
        self.config['pending_magnets'] = {}
        self.config.save()
        log.info('NextDrive disabled')

    def update(self):
        pass

    # ------------------------------------------------------------------ #
    # Hooks into TorrentManager
    # ------------------------------------------------------------------ #
    def _wrap_add(self, original):
        def add(torrent_info=None, state=None, options=None, save_state=True,
                filedump=None, filename=None, magnet=None, resume_data=None):
            options = self._pre_add(torrent_info, state, options, filedump,
                                    magnet, resume_data)
            self._holding = bool(
                magnet and not torrent_info and not filedump and state is None
                and self._should_hold_magnet(options)
            )
            try:
                return original(torrent_info=torrent_info, state=state,
                                options=options, save_state=save_state,
                                filedump=filedump, filename=filename,
                                magnet=magnet, resume_data=resume_data)
            finally:
                self._holding = False

        return add

    def _wrap_add_async(self, original):
        def add_async(torrent_info=None, state=None, options=None, save_state=True,
                      filedump=None, filename=None, magnet=None, resume_data=None):
            options = self._pre_add(torrent_info, state, options, filedump,
                                    magnet, resume_data)
            self._holding = bool(
                magnet and not torrent_info and not filedump and state is None
                and self._should_hold_magnet(options)
            )
            try:
                return original(torrent_info=torrent_info, state=state,
                                options=options, save_state=save_state,
                                filedump=filedump, filename=filename,
                                magnet=magnet, resume_data=resume_data)
            finally:
                self._holding = False

        return add_async

    def _wrap__build_torrent_params(self, original):
        def _build_torrent_params(torrent_info=None, magnet=None, options=None,
                                  resume_data=None):
            torrent_id, params = original(torrent_info, magnet, options, resume_data)
            if self._holding and magnet and not torrent_info:
                params['flags'] = params['flags'] | lt.torrent_flags.upload_mode
                self.config['pending_magnets'][torrent_id] = {
                    'was_paused': bool(options['add_paused']),
                    'pause_if_no_fit': bool(self.config['pause_if_no_fit']),
                }
                self.config.save()
                log.info('NextDrive: holding magnet %s until metadata arrives',
                         torrent_id)
            return torrent_id, params

        return _build_torrent_params

    # ------------------------------------------------------------------ #
    # Decision helpers
    # ------------------------------------------------------------------ #
    def _default_location(self):
        return self.core_config['download_location']

    def _requested_location(self, options):
        return (options or {}).get('download_location') or self._default_location()

    def _eligible(self, options):
        if not self.config['enabled'] or not self._locations():
            return False
        if self.config['only_default_location']:
            if _norm(self._requested_location(options)) != _norm(
                self._default_location()
            ):
                log.info('NextDrive: torrent added with its own path, leaving it')
                return False
        return True

    def _should_hold_magnet(self, options):
        return bool(
            self.config['handle_magnets']
            and not (options or {}).get('seed_mode')
            and self._eligible(options)
        )

    def _locations(self):
        return [loc for loc in self.config['locations'] if loc.get('path')]

    def _reserve_bytes(self, loc):
        reserve = loc.get('reserve_gb')
        if reserve in (None, ''):
            reserve = self.config['reserve_gb']
        return int(float(reserve) * GIB)

    def _committed_by_dev(self, exclude=None):
        """Bytes still to be written by unfinished torrents, per device."""
        committed = {}
        if not self.config['count_committed']:
            return committed
        for torrent_id, torrent in list(self.tm.torrents.items()):
            self._inflight.pop(torrent_id, None)
            if torrent_id == exclude:
                continue
            try:
                if torrent.options.get('pre_allocate_storage'):
                    continue  # space already taken on disk
                status = torrent.handle.status()
                if not status.has_metadata:
                    continue
                remaining = status.total_wanted - status.total_wanted_done
                if remaining <= 0:
                    continue
                dev = _dev_of(status.save_path)
            except RuntimeError:
                continue
            committed[dev] = committed.get(dev, 0) + remaining
        for torrent_id, (dev, size) in self._inflight.items():
            if torrent_id != exclude:
                committed[dev] = committed.get(dev, 0) + size
        return committed

    def _drive_report(self, size=0, exclude=None):
        committed = self._committed_by_dev(exclude)
        report = []
        for loc in self._locations():
            path = _norm(loc['path'])
            entry = {'path': path, 'ok': False, 'free': 0, 'committed': 0,
                     'reserve': self._reserve_bytes(loc), 'label': loc.get('label') or ''}
            if not os.path.isdir(path) or not os.access(path, os.W_OK):
                entry['error'] = 'missing or not writable'
                report.append(entry)
                continue
            dev = _dev_of(path)
            entry['free'] = shutil.disk_usage(path).free
            entry['committed'] = committed.get(dev, 0)
            entry['available'] = entry['free'] - entry['committed']
            entry['fits'] = entry['available'] >= size + entry['reserve']
            entry['ok'] = True
            entry['dev'] = dev
            report.append(entry)
        return report

    def _choose(self, size, exclude=None):
        for entry in self._drive_report(size, exclude):
            if not entry['ok']:
                log.warning('NextDrive: skipping %s (%s)', entry['path'], entry['error'])
                continue
            log.info(
                'NextDrive: %s free=%s committed=%s reserve=%s torrent=%s -> %s',
                entry['path'], _h(entry['free']), _h(entry['committed']),
                _h(entry['reserve']), _h(size),
                'fits' if entry['fits'] else 'no room',
            )
            if entry['fits']:
                return entry
        return None

    def _label_core(self):
        try:
            return component.get('CorePlugin.Label')
        except KeyError:
            return None

    def _label_blocks(self, label_id):
        """True if the label has its own path and we must not override it."""
        if not label_id or self.config['override_label_paths']:
            return False
        label_core = self._label_core()
        if not label_core:
            return False
        opts = label_core.labels.get(label_id, {})
        if opts.get('apply_move_completed') and opts.get('move_completed') \
                and opts.get('move_completed_path'):
            log.info('NextDrive: label "%s" has its own path, leaving torrent alone',
                     label_id)
            return True
        return False

    def _predict_label(self, torrent_info):
        """Label the Label plugin will auto-apply from trackers, if any."""
        label_core = self._label_core()
        if not label_core or torrent_info is None:
            return None
        try:
            urls = [t.url for t in torrent_info.trackers()]
        except (AttributeError, RuntimeError):
            return None
        for label_id, opts in label_core.labels.items():
            if opts.get('auto_add'):
                for match in opts.get('auto_add_trackers', []):
                    if any(match in url for url in urls):
                        return label_id
        return None

    def _schedule_label(self, torrent_id, entry):
        label_id = (entry.get('label') or '').strip().lower()
        if label_id:
            reactor.callLater(max(0, int(self.config['label_delay'])),
                              self._apply_label, torrent_id, label_id)

    def _apply_label(self, torrent_id, label_id):
        label_core = self._label_core()
        if not label_core or torrent_id not in self.tm.torrents:
            return
        if label_core.torrent_labels.get(torrent_id):
            return  # the adding client already labelled it; keep that
        try:
            if label_id not in label_core.labels:
                label_core.add(label_id)
            label_core.set_torrent(torrent_id, label_id)
            log.info('NextDrive: labelled %s as "%s"', torrent_id, label_id)
        except Exception as ex:  # Label raises plain Exception on bad input
            log.warning('NextDrive: could not set label "%s": %s', label_id, ex)

    @staticmethod
    def _wanted_size(torrent_info, options):
        prios = (options or {}).get('file_priorities') or []
        files = torrent_info.files()
        total = 0
        for index in range(torrent_info.num_files()):
            prio = prios[index] if index < len(prios) else 1
            if prio:
                total += files.file_size(index)
        return total

    # ------------------------------------------------------------------ #
    # .torrent files: decide before libtorrent sees the torrent
    # ------------------------------------------------------------------ #
    def _pre_add(self, torrent_info, state, options, filedump, magnet, resume_data):
        if state is not None or resume_data is not None:
            return options  # loading the session; never touch existing torrents
        try:
            if torrent_info is None and filedump:
                torrent_info = lt.torrent_info(lt.bdecode(filedump))
            if torrent_info is None:
                return options  # magnet without metadata: handled later
            options = dict(options or {})
            if options.get('seed_mode'):
                return options  # data already exists where the caller said
            if not self._eligible(options):
                return options
            torrent_id = str(torrent_info.info_hash())
            if torrent_id in self.tm.torrents:
                return options
            requested = self._requested_location(options)
            name = options.get('name') or torrent_info.name()
            if name and os.path.exists(os.path.join(requested, name)):
                log.info('NextDrive: data for "%s" already exists in %s, '
                         'leaving it there', name, requested)
                return options
            if self._label_blocks(self._predict_label(torrent_info)):
                return options

            size = self._wanted_size(torrent_info, options)
            entry = self._choose(size)
            if entry is None:
                log.warning('NextDrive: no location has room for "%s" (%s); '
                            'leaving it on %s', name, _h(size), requested)
                if self.config['pause_if_no_fit']:
                    options['add_paused'] = True
                return options
            self._inflight[torrent_id] = (entry['dev'], size)
            if _norm(entry['path']) != _norm(requested):
                options['download_location'] = entry['path']
            log.info('NextDrive: "%s" (%s) -> %s', name, _h(size),
                     entry['path'])
            reactor.callLater(0, self._schedule_label, torrent_id, entry)
            return options
        except Exception as ex:  # never block an add because of NextDrive
            log.exception('NextDrive: pre-add check failed, adding normally: %s', ex)
            return options

    # ------------------------------------------------------------------ #
    # Magnets: decide when metadata arrives, while still at 0 bytes
    # ------------------------------------------------------------------ #
    def _on_metadata_received(self, alert):
        try:
            torrent_id = str(alert.handle.info_hash())
        except RuntimeError:
            return
        info = self.config['pending_magnets'].get(torrent_id)
        if info is not None:
            self._decide_magnet(torrent_id, info)

    def _decide_magnet(self, torrent_id, info):
        self.config['pending_magnets'].pop(torrent_id, None)
        self.config.save()
        torrent = self.tm.torrents.get(torrent_id)
        if torrent is None:
            return
        keep_paused = info.get('was_paused', False)
        try:
            status = torrent.handle.status()
            if status.total_done > 0:
                log.warning('NextDrive: magnet %s already has data, not moving it',
                            torrent_id)
                return
            if self._label_blocks(self._label_core() and
                                  self._label_core().torrent_labels.get(torrent_id)):
                return
            size = status.total_wanted
            current = status.save_path
            entry = self._choose(size, exclude=torrent_id)
            if entry is None:
                log.warning('NextDrive: no location has room for magnet "%s" '
                            '(%s); leaving it on %s', status.name,
                            _h(size), current)
                if info.get('pause_if_no_fit'):
                    keep_paused = True
                return
            if _norm(entry['path']) != _norm(current):
                torrent.move_storage(entry['path'])
            log.info('NextDrive: magnet "%s" (%s) -> %s', status.name,
                     _h(size), entry['path'])
            self._schedule_label(torrent_id, entry)
        except Exception as ex:
            log.exception('NextDrive: magnet decision failed: %s', ex)
        finally:
            self._release(torrent_id, {'was_paused': keep_paused})

    def _release(self, torrent_id, info):
        torrent = self.tm.torrents.get(torrent_id)
        if torrent is None:
            return
        try:
            if info.get('was_paused'):
                torrent.pause()
            torrent.handle.unset_flags(lt.torrent_flags.upload_mode)
        except RuntimeError as ex:
            log.warning('NextDrive: could not release %s: %s', torrent_id, ex)

    def _rearm_pending(self, *args):
        """After a restart, re-hold magnets that were waiting for metadata."""
        for torrent_id, info in list(self.config['pending_magnets'].items()):
            torrent = self.tm.torrents.get(torrent_id)
            if torrent is None:
                continue
            try:
                if torrent.handle.status().has_metadata:
                    self._decide_magnet(torrent_id, info)
                else:
                    torrent.handle.set_flags(lt.torrent_flags.upload_mode)
            except RuntimeError:
                pass

    def _on_torrent_removed(self, torrent_id):
        self._inflight.pop(torrent_id, None)
        if self.config['pending_magnets'].pop(torrent_id, None) is not None:
            self.config.save()

    # ------------------------------------------------------------------ #
    # RPC
    # ------------------------------------------------------------------ #
    @export
    def get_config(self):
        return {k: self.config[k] for k in DEFAULT_PREFS if k != 'pending_magnets'}

    @export
    def set_config(self, config):
        for key, value in config.items():
            if key == 'pending_magnets' or key not in DEFAULT_PREFS:
                continue
            if key == 'locations':
                clean, seen = [], set()
                for loc in value or []:
                    path = (loc.get('path') or '').strip()
                    if not path or _norm(path) in seen:
                        continue
                    seen.add(_norm(path))
                    reserve = loc.get('reserve_gb')
                    reserve = None if reserve in (None, '') else max(0.0, float(reserve))
                    clean.append({'path': _norm(path), 'reserve_gb': reserve,
                                  'label': (loc.get('label') or '').strip().lower()})
                value = clean
            elif key == 'reserve_gb':
                value = max(0.0, float(value))
            elif key == 'label_delay':
                value = max(0, int(value))
            else:
                value = bool(value)
            self.config[key] = value
        self.config.save()

    @export
    def get_status(self, size=0):
        """Per-location free/committed/fit report (sizes in bytes)."""
        report = self._drive_report(int(size or 0))
        for entry in report:
            entry.pop('dev', None)
        return report
