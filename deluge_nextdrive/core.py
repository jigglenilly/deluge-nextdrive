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

A location can also have a completed folder. A torrent placed on a location
then gets its move-completed path on the same drive: a path under the first
location's completed folder is rewritten to the chosen location's completed
folder, keeping any subfolder (such as a Label folder). This only applies to
torrents NextDrive placed, and it is kept when a label is set later.
"""

import logging
import math
import os
import re
import shutil
import time

import deluge.component as component
import deluge.configmanager
from deluge._libtorrent import lt
from deluge.core.rpcserver import export
from deluge.error import DelugeError
from deluge.plugins.pluginbase import CorePluginBase
from twisted.internet import reactor

log = logging.getLogger(__name__)

GIB = 1024**3

DEFAULT_PREFS = {
    # Master switch.
    'enabled': True,
    # Ordered list of {'path': str, 'reserve_gb': float or None, 'label': str,
    # 'completed': str}. reserve_gb None/blank means "use the global
    # reserve_gb". completed '' means "leave move-completed paths alone".
    'locations': [],
    # Global free-space reserve in GiB kept on every location.
    'reserve_gb': 5.0,
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
    # Internal: torrents placed on a location with a completed folder
    # {torrent_id: location path}, so their move-completed path stays on
    # that drive across label changes and restarts.
    'placed': {},
}

INTERNAL_KEYS = ('pending_magnets', 'placed')


# Same rule the Label plugin uses for label names.
LABEL_RE = re.compile(r'[a-z0-9_\-\.]*\Z')
INFLIGHT_TTL = 300  # seconds an add may take before its reservation is dropped


# Errors must use a class the clients know (deluge.error), or the Web UI and
# other clients cannot rebuild the error and the call hangs.
NextDriveError = DelugeError


def _norm(path):
    return os.path.normpath(os.path.expanduser(path or '')).rstrip(os.sep) or os.sep


def _to_bool(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
            'true', 'yes', 'on', '1', 'false', 'no', 'off', '0'):
        return value.strip().lower() in ('true', 'yes', 'on', '1')
    raise NextDriveError('%s must be true or false, got %r' % (name, value))


def _to_number(value, name, cast=float):
    if isinstance(value, bool) or value is None:
        raise NextDriveError('%s must be a number, got %r' % (name, value))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise NextDriveError('%s must be a number, got %r' % (name, value))
    if not math.isfinite(number):
        raise NextDriveError('%s must be a finite number, got %r' % (name, value))
    return cast(max(0, number))


def _clean_location(loc):
    """Validate one location entry. Accepts a dict or a bare path string."""
    if isinstance(loc, str):
        loc = {'path': loc}
    if not isinstance(loc, dict):
        raise NextDriveError('location must be a path or an object, got %r' % (loc,))
    path = loc.get('path')
    if not isinstance(path, str) or not path.strip():
        raise NextDriveError('location has no path: %r' % (loc,))
    path = path.strip()
    if not os.path.isabs(os.path.expanduser(path)):
        raise NextDriveError('location path must be absolute: %r' % path)
    reserve = loc.get('reserve_gb')
    if isinstance(reserve, str) and not reserve.strip():
        reserve = None
    if reserve is not None:
        reserve = _to_number(reserve, 'reserve_gb for %s' % path)
    label = loc.get('label') or ''
    if not isinstance(label, str):
        raise NextDriveError('label for %s must be text, got %r' % (path, label))
    label = label.strip().lower()
    if not LABEL_RE.match(label):
        raise NextDriveError('label %r for %s may only use a-z 0-9 _ - .' % (label, path))
    completed = loc.get('completed') or ''
    if not isinstance(completed, str):
        raise NextDriveError('completed folder for %s must be text, got %r'
                             % (path, completed))
    completed = completed.strip()
    if completed:
        if not os.path.isabs(os.path.expanduser(completed)):
            raise NextDriveError('completed folder must be absolute: %r' % completed)
        completed = _norm(completed)
    return {'path': _norm(path), 'reserve_gb': reserve, 'label': label,
            'completed': completed}


def _clean_locations(value, lenient=False):
    if not isinstance(value, (list, tuple)):
        if lenient:
            log.warning('NextDrive: ignoring bad locations setting %r', value)
            return []
        raise NextDriveError('locations must be a list, got %r' % (value,))
    clean, seen = [], set()
    for loc in value:
        try:
            entry = _clean_location(loc)
        except NextDriveError as ex:
            if not lenient:
                raise
            try:  # keep the drive, drop its bad reserve/label/completed
                entry = _clean_location({'path': loc.get('path')} if isinstance(loc, dict) else loc)
                log.warning('NextDrive: %s; using defaults for %s', ex, entry['path'])
            except NextDriveError:
                log.warning('NextDrive: ignoring bad location in config: %s', ex)
                continue
        if entry['path'] in seen:
            continue
        seen.add(entry['path'])
        clean.append(entry)
    return clean


def _clean_value(key, value, lenient=False):
    if key == 'locations':
        return _clean_locations(value, lenient)
    if key == 'reserve_gb':
        return _to_number(value, key)
    if key == 'label_delay':
        return _to_number(value, key, int)
    if key in INTERNAL_KEYS:
        return value if isinstance(value, dict) else {}
    return _to_bool(value, key)


def _h(num):
    """Human-readable size for the log."""
    if abs(num) >= GIB:
        return '%.2f GiB' % (num / GIB)
    return '%.1f MiB' % (num / 1024**2)


def _under(path, root):
    """Part of path below root ('' for root itself), or None if not under it."""
    path, root = _norm(path), _norm(root)
    if path == root:
        return ''
    prefix = root if root.endswith(os.sep) else root + os.sep
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


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
        self._sanitize_config()
        self.tm = component.get('TorrentManager')
        self.core_config = component.get('Core').config
        self._inflight = {}  # torrent_id -> (st_dev, bytes, time) chosen, not yet in tm
        self._holding = False
        self._wrapped = {}
        self._to_place = {}  # torrent_id -> (location path, time), added soon
        self._hooked = {}  # torrent_id -> original set_move_completed_path
        self._moving = {}  # torrent_id -> move_completed to restore after our move

        for name in ('add', 'add_async', '_build_torrent_params'):
            original = getattr(self.tm, name)
            had_attr = name in self.tm.__dict__
            wrapper = getattr(self, '_wrap_' + name)(original)
            self._wrapped[name] = (original, had_attr, wrapper)
            setattr(self.tm, name, wrapper)

        am = component.get('AlertManager')
        am.register_handler('metadata_received', self._on_metadata_received)
        am.register_handler('storage_moved', self._on_storage_moved)
        em = component.get('EventManager')
        em.register_event_handler('TorrentRemovedEvent', self._on_torrent_removed)
        em.register_event_handler('TorrentAddedEvent', self._on_torrent_added)
        em.register_event_handler('SessionStartedEvent', self._rearm_pending)
        em.register_event_handler('SessionStartedEvent', self._prune_placed)
        reactor.callLater(0, self._rearm_pending)
        reactor.callLater(0, self._hook_placed)
        log.info(
            'NextDrive enabled with %d location(s)', len(self.config['locations'])
        )

    def _sanitize_config(self):
        """Repair a hand-edited, old or damaged config instead of failing later."""
        changed = False
        for key, default in DEFAULT_PREFS.items():
            try:
                value = _clean_value(key, self.config[key], lenient=True)
            except NextDriveError as ex:
                log.warning('NextDrive: bad %s in config (%s), using %r', key, ex, default)
                value = default
            if value != self.config[key] or type(value) is not type(self.config[key]):
                self._put(key, value)
                changed = True
        if changed:
            self.config.save()

    def _put(self, key, value):
        # Deluge's Config casts new values to the stored type (a stored int
        # would turn 2.5 into 2, a stored list would turn {} into []).
        if key in self.config.config and type(self.config[key]) is not type(value):
            del self.config[key]
        self.config[key] = value

    def disable(self):
        shutting_down = getattr(self.tm, '_component_state', 'Started') != 'Started'
        for name, (original, had_attr, wrapper) in self._wrapped.items():
            if getattr(self.tm, name, None) is wrapper:
                if had_attr:
                    setattr(self.tm, name, original)
                else:
                    delattr(self.tm, name)
        self._wrapped = {}
        component.get('AlertManager').deregister_handler(self._on_metadata_received)
        component.get('AlertManager').deregister_handler(self._on_storage_moved)
        em = component.get('EventManager')
        em.deregister_event_handler('TorrentRemovedEvent', self._on_torrent_removed)
        em.deregister_event_handler('TorrentAddedEvent', self._on_torrent_added)
        em.deregister_event_handler('SessionStartedEvent', self._rearm_pending)
        em.deregister_event_handler('SessionStartedEvent', self._prune_placed)
        for torrent_id in list(self._hooked):
            self._unhook(torrent_id)
        if shutting_down:
            # Daemon is stopping: keep held magnets on hold and remember them,
            # so the decision is still made after the restart.
            self.config.save()
        else:
            # Plugin switched off by the user: never leave a magnet stuck on hold.
            for torrent_id in list(self.config['pending_magnets']):
                self._release(torrent_id)
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
            options, reserved = self._pre_add(torrent_info, state, options,
                                              filedump, magnet, resume_data)
            self._holding = bool(
                magnet and not torrent_info and not filedump and state is None
                and self._should_hold_magnet(options)
            )
            try:
                result = original(torrent_info=torrent_info, state=state,
                                  options=options, save_state=save_state,
                                  filedump=filedump, filename=filename,
                                  magnet=magnet, resume_data=resume_data)
            except Exception:
                self._inflight.pop(reserved, None)
                raise
            finally:
                self._holding = False
            if reserved and hasattr(result, 'addErrback'):
                def drop(failure):
                    self._inflight.pop(reserved, None)
                    return failure
                result.addErrback(drop)
            return result

        return add

    def _wrap_add_async(self, original):
        def add_async(torrent_info=None, state=None, options=None, save_state=True,
                      filedump=None, filename=None, magnet=None, resume_data=None):
            options, reserved = self._pre_add(torrent_info, state, options,
                                              filedump, magnet, resume_data)
            self._holding = bool(
                magnet and not torrent_info and not filedump and state is None
                and self._should_hold_magnet(options)
            )
            try:
                result = original(torrent_info=torrent_info, state=state,
                                  options=options, save_state=save_state,
                                  filedump=filedump, filename=filename,
                                  magnet=magnet, resume_data=resume_data)
            except Exception:
                self._inflight.pop(reserved, None)
                raise
            finally:
                self._holding = False
            if reserved and hasattr(result, 'addErrback'):
                def drop(failure):
                    self._inflight.pop(reserved, None)
                    return failure
                result.addErrback(drop)
            return result

        return add_async

    def _wrap__build_torrent_params(self, original):
        def _build_torrent_params(torrent_info=None, magnet=None, options=None,
                                  resume_data=None):
            torrent_id, params = original(torrent_info, magnet, options, resume_data)
            if self._holding and magnet and not torrent_info:
                params['flags'] = params['flags'] | lt.torrent_flags.upload_mode
                self.config['pending_magnets'][torrent_id] = {
                    'pause_if_no_fit': bool(self.config['pause_if_no_fit']),
                }
                self.config.save()
                log.info('NextDrive: holding magnet %s until metadata arrives',
                         torrent_id)
            elif torrent_id in self.config['pending_magnets']:
                # Reloaded after a restart before its decision was made: keep
                # it on hold from the very first moment.
                params['flags'] = params['flags'] | lt.torrent_flags.upload_mode
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
        try:
            return bool(
                self.config['handle_magnets']
                and not (options or {}).get('seed_mode')
                and self._eligible(options)
            )
        except Exception as ex:  # never block an add because of NextDrive
            log.warning('NextDrive: magnet check failed, adding normally: %s', ex)
            return False

    def _locations(self):
        return [loc for loc in self.config['locations']
                if isinstance(loc, dict) and loc.get('path')]

    def _reserve_bytes(self, loc):
        reserve = loc.get('reserve_gb')
        if reserve in (None, ''):
            reserve = self.config['reserve_gb']
        return int(float(reserve) * GIB)

    def _existing_data(self, name, requested):
        """Folder (requested first, then each location) that already holds name."""
        if not name:
            return None
        for folder in [requested] + [loc['path'] for loc in self._locations()]:
            if os.path.exists(os.path.join(_norm(folder), name)):
                return folder
        return None

    def _committed_by_dev(self, exclude=None):
        """Bytes still to be written by unfinished torrents, per device."""
        committed = {}
        if not self.config['count_committed']:
            return committed
        pending = self.config['pending_magnets']
        now = time.monotonic()
        for torrent_id, (_dev, _size, since) in list(self._inflight.items()):
            if now - since > INFLIGHT_TTL:
                self._inflight.pop(torrent_id, None)
        for torrent_id, torrent in list(self.tm.torrents.items()):
            self._inflight.pop(torrent_id, None)
            if torrent_id == exclude or torrent_id in pending:
                continue  # undecided magnets are counted when they are placed
            try:
                if torrent.options.get('pre_allocate_storage'):
                    continue  # space already taken on disk
                status = torrent.handle.status()
                if not status.has_metadata:
                    continue
                remaining = self._remaining(torrent, status)
                if remaining <= 0:
                    continue
                # A just-placed magnet is still moving: count it at its target.
                dev = _dev_of(getattr(torrent, 'moving_storage_dest_path', None)
                              or status.save_path)
            except RuntimeError:
                continue
            committed[dev] = committed.get(dev, 0) + remaining
        for torrent_id, (dev, size, _since) in self._inflight.items():
            if torrent_id != exclude:
                committed[dev] = committed.get(dev, 0) + size
        return committed

    def _drive_report(self, size=0, exclude=None):
        committed = self._committed_by_dev(exclude)
        report = []
        for loc in self._locations():
            path = _norm(loc['path'])
            entry = {'path': path, 'ok': False, 'free': 0, 'committed': 0,
                     'reserve': self._reserve_bytes(loc), 'label': loc.get('label') or '',
                     'completed': loc.get('completed') or ''}
            if not os.path.isdir(path) or not os.access(path, os.W_OK):
                entry['error'] = 'missing or not writable'
                report.append(entry)
                continue
            dev = _dev_of(path)
            try:
                entry['free'] = shutil.disk_usage(path).free
            except OSError as ex:
                entry['error'] = 'cannot read free space: %s' % ex
                report.append(entry)
                continue
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
            if self._mappable(opts['move_completed_path']):
                return False  # kept on the chosen drive by its completed folder
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
            if files.file_flags(index) & lt.file_storage.flag_pad_file:
                continue  # padding is never written to disk
            prio = prios[index] if index < len(prios) else 1
            if prio:
                total += files.file_size(index)
        return total

    def _remaining(self, torrent, status):
        """Bytes this torrent still has to write.

        Right after an add (or when metadata has only just arrived) libtorrent
        can report total_wanted as 0, so fall back to the torrent's file list.
        """
        total = status.total_wanted
        if total <= 0:
            info = torrent.handle.torrent_file()
            if info is None:
                return 0
            total = self._wanted_size(info, {
                'file_priorities': torrent.options.get('file_priorities')})
        return max(0, total - status.total_wanted_done)

    # ------------------------------------------------------------------ #
    # .torrent files: decide before libtorrent sees the torrent
    # ------------------------------------------------------------------ #
    def _pre_add(self, torrent_info, state, options, filedump, magnet, resume_data):
        if state is not None or resume_data is not None:
            return options, None  # loading the session; never touch existing torrents
        try:
            return self._pre_add_checked(torrent_info, options, filedump)
        except Exception as ex:  # never block an add because of NextDrive
            log.exception('NextDrive: pre-add check failed, adding normally: %s', ex)
            return options, None

    def _pre_add_checked(self, torrent_info, options, filedump):
        if torrent_info is None and filedump:
            torrent_info = lt.torrent_info(lt.bdecode(filedump))
        if torrent_info is None:
            return options, None  # magnet without metadata: handled later
        options = dict(options or {})
        if options.get('seed_mode'):
            return options, None  # data already exists where the caller said
        if not self._eligible(options):
            return options, None
        torrent_id = str(torrent_info.info_hash())
        if torrent_id in self.tm.torrents:
            return options, None
        requested = self._requested_location(options)
        name = torrent_info.name()
        existing = self._existing_data(name, requested)
        if existing is not None:
            if _norm(existing) != _norm(requested):
                options['download_location'] = existing
            log.info('NextDrive: data for "%s" already exists in %s, using it',
                     name, existing)
            self._will_place(torrent_id, existing)
            return options, None
        if self._label_blocks(self._predict_label(torrent_info)):
            return options, None

        size = self._wanted_size(torrent_info, options)
        entry = self._choose(size)
        if entry is None:
            log.warning('NextDrive: no location has room for "%s" (%s); '
                        'leaving it on %s', name, _h(size), requested)
            if self.config['pause_if_no_fit']:
                options['add_paused'] = True
            return options, None
        self._inflight[torrent_id] = (entry['dev'], size, time.monotonic())
        if _norm(entry['path']) != _norm(requested):
            options['download_location'] = entry['path']
        log.info('NextDrive: "%s" (%s) -> %s', name, _h(size), entry['path'])
        self._will_place(torrent_id, entry['path'])
        reactor.callLater(0, self._schedule_label, torrent_id, entry)
        return options, torrent_id

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
        pause = False
        try:
            status = torrent.handle.status()
            if status.total_done > 0:
                log.warning('NextDrive: magnet %s already has data, not moving it',
                            torrent_id)
                return
            current = status.save_path
            existing = self._existing_data(status.name, current)
            if existing is not None:
                if _norm(existing) != _norm(current):
                    self._move(torrent, torrent_id, existing)
                log.info('NextDrive: data for magnet "%s" already exists in %s, '
                         'using it', status.name, existing)
                self._place(torrent_id, existing)
                return
            label_core = self._label_core()
            if self._label_blocks(label_core and label_core.torrent_labels.get(torrent_id)):
                return
            size = self._remaining(torrent, status)
            entry = self._choose(size, exclude=torrent_id)
            if entry is None:
                log.warning('NextDrive: no location has room for magnet "%s" '
                            '(%s); leaving it on %s', status.name,
                            _h(size), current)
                pause = bool(info.get('pause_if_no_fit'))
                return
            if _norm(entry['path']) != _norm(current):
                self._move(torrent, torrent_id, entry['path'])
            log.info('NextDrive: magnet "%s" (%s) -> %s', status.name,
                     _h(size), entry['path'])
            self._place(torrent_id, entry['path'])
            self._schedule_label(torrent_id, entry)
        except Exception as ex:
            log.exception('NextDrive: magnet decision failed: %s', ex)
        finally:
            self._release(torrent_id, pause)

    def _move(self, torrent, torrent_id, dest):
        # Deluge switches move-completed off after any storage move, which
        # would leave a placed magnet in its download folder forever. Remember
        # the setting and put it back once the (empty) move is done.
        self._moving[torrent_id] = bool(torrent.options.get('move_completed'))
        if not torrent.move_storage(dest):
            self._moving.pop(torrent_id, None)

    def _on_storage_moved(self, alert):
        try:
            torrent_id = str(alert.handle.info_hash())
        except RuntimeError:
            return
        if torrent_id not in self._moving:
            return
        move_completed = self._moving.pop(torrent_id)
        torrent = self.tm.torrents.get(torrent_id)
        if torrent is not None and move_completed:
            torrent.set_move_completed(True)

    def _release(self, torrent_id, pause=False):
        torrent = self.tm.torrents.get(torrent_id)
        if torrent is None:
            return
        try:
            if pause:
                torrent.pause()
            torrent.handle.unset_flags(lt.torrent_flags.upload_mode)
            # libtorrent can report a held magnet as finished when its metadata
            # arrives. Deluge then marks it finished with nothing downloaded and
            # would never move it to its move-completed path. Undo that.
            if getattr(torrent, 'is_finished', False) \
                    and not torrent.handle.status().is_finished:
                torrent.is_finished = False
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
        self._to_place.pop(torrent_id, None)
        self._hooked.pop(torrent_id, None)
        self._moving.pop(torrent_id, None)
        changed = self.config['pending_magnets'].pop(torrent_id, None) is not None
        changed = self.config['placed'].pop(torrent_id, None) is not None or changed
        if changed:
            self.config.save()

    # ------------------------------------------------------------------ #
    # Completed folders: keep move-completed on the drive that was chosen
    # ------------------------------------------------------------------ #
    def _location(self, path):
        for loc in self._locations():
            if _norm(loc['path']) == _norm(path):
                return loc
        return None

    def _mappable(self, completed_path):
        """True if completed_path is under the first location's completed folder."""
        locs = self._locations()
        first = locs[0].get('completed') if locs else ''
        return bool(first and completed_path and _under(completed_path, first) is not None)

    def _map_completed(self, completed_path, location_path):
        """completed_path moved onto location_path's drive, or unchanged."""
        locs = self._locations()
        loc = self._location(location_path)
        if not locs or loc is None or not completed_path:
            return completed_path
        first, target = locs[0].get('completed'), loc.get('completed')
        if not first or not target or _norm(first) == _norm(target):
            return completed_path
        rest = _under(completed_path, first)
        if rest is None:
            return completed_path
        return os.path.join(target, rest) if rest else target

    def _wants_place(self, location_path):
        loc = self._location(location_path)
        locs = self._locations()
        return bool(loc and loc.get('completed') and locs[0].get('completed')
                    and _norm(loc['path']) != _norm(locs[0]['path']))

    def _will_place(self, torrent_id, location_path):
        """A .torrent is about to be added on location_path."""
        if self._wants_place(location_path):
            self._to_place[torrent_id] = (location_path, time.monotonic())

    def _on_torrent_added(self, torrent_id, from_state=False):
        if from_state:
            return
        now = time.monotonic()
        for other, (_path, since) in list(self._to_place.items()):
            if now - since > INFLIGHT_TTL:
                self._to_place.pop(other, None)
        placed = self._to_place.pop(torrent_id, None)
        if placed is not None:
            self._place(torrent_id, placed[0])

    def _place(self, torrent_id, location_path):
        if not self._wants_place(location_path):
            return
        try:
            self.config['placed'][torrent_id] = _norm(location_path)
            self.config.save()
            self._hook(torrent_id)
        except Exception as ex:  # never break an add because of NextDrive
            log.exception('NextDrive: could not set completed folder: %s', ex)

    def _hook(self, torrent_id):
        """Keep this torrent's move-completed path on its drive, now and later."""
        torrent = self.tm.torrents.get(torrent_id)
        if torrent is None:
            return
        if torrent_id not in self._hooked:
            original = torrent.set_move_completed_path

            def set_move_completed_path(path, _id=torrent_id, _orig=original):
                location = self.config['placed'].get(_id)
                if location:
                    mapped = self._map_completed(path, location)
                    if mapped != path:
                        log.info('NextDrive: %s completes to %s instead of %s',
                                 _id, mapped, path)
                        path = mapped
                return _orig(path)

            torrent.set_move_completed_path = set_move_completed_path
            self._hooked[torrent_id] = original
        current = torrent.options.get('move_completed_path')
        torrent.set_move_completed_path(current)

    def _unhook(self, torrent_id):
        self._hooked.pop(torrent_id, None)
        torrent = self.tm.torrents.get(torrent_id)
        if torrent is not None and 'set_move_completed_path' in torrent.__dict__:
            del torrent.set_move_completed_path

    def _hook_placed(self, *args):
        for torrent_id in list(self.config['placed']):
            try:
                self._hook(torrent_id)
            except Exception as ex:
                log.warning('NextDrive: could not keep completed folder for %s: %s',
                            torrent_id, ex)

    def _prune_placed(self, *args):
        """After all torrents are loaded: forget removed ones, hook the rest."""
        placed = self.config['placed']
        gone = [t for t in placed if t not in self.tm.torrents]
        for torrent_id in gone:
            placed.pop(torrent_id, None)
        if gone:
            self.config.save()
        self._hook_placed()

    # ------------------------------------------------------------------ #
    # RPC
    # ------------------------------------------------------------------ #
    @export
    def get_config(self):
        return {k: self.config[k] for k in DEFAULT_PREFS if k not in INTERNAL_KEYS}

    @export
    def set_config(self, config):
        """Validate everything first; on any bad value nothing is changed."""
        if not isinstance(config, dict):
            raise NextDriveError('config must be an object')
        clean = {}
        for key, value in config.items():
            if key in INTERNAL_KEYS or key not in DEFAULT_PREFS:
                continue
            clean[key] = _clean_value(key, value)
        for key, value in clean.items():
            self._put(key, value)
        self.config.save()

    @export
    def get_status(self, size=0):
        """Per-location free/committed/fit report (sizes in bytes)."""
        report = self._drive_report(_to_number(size or 0, 'size', int))
        for entry in report:
            entry.pop('dev', None)
        return report
