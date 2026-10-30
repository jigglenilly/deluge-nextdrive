/**
 * NextDrive web UI preferences page.
 * Copyright (C) 2026 jigglenilly
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
Ext.ns('Deluge.ux.preferences');

Deluge.ux.preferences.NextDrivePage = Ext.extend(Ext.Panel, {
    title: _('NextDrive'),
    header: false,
    border: false,
    layout: 'border',

    initComponent: function () {
        Deluge.ux.preferences.NextDrivePage.superclass.initComponent.call(this);

        this.store = new Ext.data.JsonStore({
            fields: ['path', 'reserve_gb', 'label', 'free'],
        });

        var self = this;
        this.grid = new Ext.grid.EditorGridPanel({
            region: 'center',
            store: this.store,
            clicksToEdit: 1,
            stripeRows: true,
            viewConfig: { forceFit: true },
            sm: new Ext.grid.RowSelectionModel({ singleSelect: true }),
            columns: [
                { header: '#', width: 28, renderer: function (v, m, r, row) { return row + 1; } },
                { header: _('Download path'), dataIndex: 'path', width: 210,
                  editor: new Ext.form.TextField({ allowBlank: false }) },
                { header: _('Reserve GiB'), dataIndex: 'reserve_gb', width: 80,
                  renderer: function (v) { return (v === null || v === '' || v === undefined) ? '<i>' + _('default') + '</i>' : v; },
                  editor: new Ext.form.NumberField({ allowBlank: true, minValue: 0, decimalPrecision: 2 }) },
                { header: _('Label'), dataIndex: 'label', width: 80,
                  editor: new Ext.form.TextField({ allowBlank: true }) },
                { header: _('Free now'), dataIndex: 'free', width: 80 },
            ],
            tbar: [
                { text: _('Add'), iconCls: 'icon-add', handler: this.onAddRow, scope: this },
                { text: _('Remove'), iconCls: 'icon-remove', handler: this.onRemoveRow, scope: this },
                '-',
                { text: _('Up'), iconCls: 'icon-up', handler: function () { self.onMove(-1); } },
                { text: _('Down'), iconCls: 'icon-down', handler: function () { self.onMove(1); } },
                '-',
                { text: _('Refresh'), handler: this.loadStatus, scope: this },
            ],
        });
        this.add(this.grid);

        this.form = this.add({
            xtype: 'form',
            region: 'south',
            height: 225,
            autoScroll: true,
            border: false,
            bodyStyle: 'padding: 6px',
            labelWidth: 150,
            items: [
                { xtype: 'label', html: _('Top row is tried first. Torrents go to the first path with room for the torrent plus its reserve. Data that has been downloaded is never moved.') + '<br><br>' },
                { xtype: 'checkbox', name: 'enabled', boxLabel: _('Enabled'), hideLabel: true },
                { xtype: 'numberfield', name: 'reserve_gb', fieldLabel: _('Default reserve (GiB)'), minValue: 0, decimalPrecision: 3, width: 60 },
                { xtype: 'checkbox', name: 'only_default_location', boxLabel: _('Only reroute torrents headed to the default download folder'), hideLabel: true },
                { xtype: 'checkbox', name: 'handle_magnets', boxLabel: _('Hold magnets until metadata arrives, then decide'), hideLabel: true },
                { xtype: 'checkbox', name: 'count_committed', boxLabel: _('Count space still owed to unfinished downloads'), hideLabel: true },
                { xtype: 'checkbox', name: 'override_label_paths', boxLabel: _('Override labels that have their own move-completed path'), hideLabel: true },
                { xtype: 'checkbox', name: 'pause_if_no_fit', boxLabel: _('Pause the torrent if nothing fits'), hideLabel: true },
            ],
        });

        this.on('show', this.loadConfig, this);
    },

    onAddRow: function () {
        var Rec = this.store.recordType;
        this.grid.stopEditing();
        this.store.add(new Rec({ path: '', reserve_gb: '', label: '', free: '' }));
        this.grid.startEditing(this.store.getCount() - 1, 1);
    },

    onRemoveRow: function () {
        var rec = this.grid.getSelectionModel().getSelected();
        if (rec) this.store.remove(rec);
        this.grid.getView().refresh();
    },

    onMove: function (dir) {
        var rec = this.grid.getSelectionModel().getSelected();
        if (!rec) return;
        var i = this.store.indexOf(rec), j = i + dir;
        if (j < 0 || j >= this.store.getCount()) return;
        this.store.remove(rec);
        this.store.insert(j, rec);
        this.grid.getView().refresh();
        this.grid.getSelectionModel().selectRow(j);
    },

    loadConfig: function () {
        deluge.client.nextdrive.get_config({
            success: function (config) {
                this.loaded = true;
                var rows = [];
                Ext.each(config.locations, function (loc) {
                    rows.push({ path: loc.path, reserve_gb: loc.reserve_gb === null ? '' : loc.reserve_gb, label: loc.label || '', free: '' });
                });
                this.store.loadData(rows);
                this.form.getForm().setValues(config);
                this.loadStatus();
            },
            scope: this,
        });
    },

    loadStatus: function () {
        deluge.client.nextdrive.get_status(0, {
            success: function (report) {
                var byPath = {};
                Ext.each(report, function (e) { byPath[e.path] = e; });
                this.store.each(function (rec) {
                    var e = byPath[rec.get('path')];
                    var txt = !e ? '' : (!e.ok ? _('unavailable') : (e.available >= 1073741824
                        ? (e.available / 1073741824).toFixed(1) + ' GiB'
                        : Math.max(0, e.available / 1048576).toFixed(0) + ' MiB'));
                    rec.set('free', txt);
                    rec.commit();
                });
            },
            scope: this,
        });
    },

    onApply: function () {
        // Preferences calls onApply on every page, even ones never opened.
        // Only save what the user could actually see and edit.
        if (!this.loaded) return;
        this.grid.stopEditing();
        var locations = [];
        this.store.each(function (rec) {
            var r = rec.get('reserve_gb');
            if (rec.get('path')) {
                locations.push({ path: rec.get('path'), reserve_gb: (r === '' || r === null || r === undefined) ? null : r, label: rec.get('label') || '' });
            }
        });
        var v = this.form.getForm().getFieldValues();
        deluge.client.nextdrive.set_config({
            enabled: v.enabled,
            reserve_gb: v.reserve_gb === '' ? 5 : v.reserve_gb,
            only_default_location: v.only_default_location,
            handle_magnets: v.handle_magnets,
            count_committed: v.count_committed,
            override_label_paths: v.override_label_paths,
            pause_if_no_fit: v.pause_if_no_fit,
            locations: locations,
        }, {
            success: this.loadConfig,
            failure: function (res) {
                var err = res && res.error;
                var msg = (err && (err.message || err.msg)) || _('Settings were not saved.');
                Ext.MessageBox.show({ title: _('NextDrive'), msg: Ext.util.Format.htmlEncode(msg),
                    buttons: Ext.MessageBox.OK, icon: Ext.MessageBox.ERROR });
                this.loadConfig();
            },
            scope: this,
        });
    },
});

Deluge.plugins.NextDrivePlugin = Ext.extend(Deluge.Plugin, {
    name: 'NextDrive',

    onDisable: function () {
        deluge.preferences.removePage(this.prefsPage);
    },

    onEnable: function () {
        this.prefsPage = deluge.preferences.addPage(new Deluge.ux.preferences.NextDrivePage());
    },
});
Deluge.registerPlugin('NextDrive', Deluge.plugins.NextDrivePlugin);
