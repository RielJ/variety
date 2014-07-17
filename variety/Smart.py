# -*- Mode: Python; coding: utf-8; indent-tabs-mode: nil; tab-width: 4 -*-
# ## BEGIN LICENSE
# Copyright (c) 2012, Peter Levi <peterlevi@peterlevi.com>
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
### END LICENSE

from gi.repository import GObject
import hashlib
from urllib2 import HTTPError
from variety.Util import Util
from variety.Options import Options
from variety.SmartFeaturesNoticeDialog import SmartFeaturesNoticeDialog
from variety.AttrDict import AttrDict
from variety.ImageFetcher import ImageFetcher

from variety import _, _u

import os
import logging
import random
import json
import base64
import threading
import time

random.seed()
logger = logging.getLogger('variety')


class Smart:
    API_URL = "http://localhost:4000"

    def __init__(self, parent):
        self.parent = parent
        self.user = None

    def reload(self):
        self.load_user(create_if_missing=False)
        self.sync()

    def first_run(self):
        if not self.parent.options.smart_notice_shown:
            self.show_notice_dialog()
        else:
            self.sync()

    def new_user(self):
        logger.info('Creating new smart user')
        self.user = Util.fetch_json(Smart.API_URL + '/newuser')
        if self.parent.preferences_dialog:
            GObject.idle_add(self.parent.preferences_dialog.on_smart_user_updated)
        with open(os.path.join(self.parent.config_folder, 'smart_user.json'), 'w') as f:
            json.dump(self.user, f, ensure_ascii=False, indent=2)
            logger.info('Created smart user: %s' % self.user["id"])

    def set_user(self, user):
        logger.info('Setting new smart user')
        self.user = user
        if self.parent.preferences_dialog:
            self.parent.preferences_dialog.on_smart_user_updated()
        with open(os.path.join(self.parent.config_folder, 'smart_user.json'), 'w') as f:
            json.dump(self.user, f, ensure_ascii=False, indent=2)
            logger.info('Updated smart user: %s' % self.user["id"])

    def load_user(self, create_if_missing=True, force_reload=False):
        if not self.user or force_reload:
            try:
                with open(os.path.join(self.parent.config_folder, 'smart_user.json')) as f:
                    self.user = json.load(f)
                    if self.parent.preferences_dialog:
                        self.parent.preferences_dialog.on_smart_user_updated()
                    logger.info('Loaded smart user: %s' % self.user["id"])
            except IOError:
                if create_if_missing:
                    logger.info('Missing user.json, creating new smart user')
                    self.new_user()

    def report_file(self, filename, tag, attempt=0):
        if not self.is_smart_enabled():
            return -1

        try:
            self.load_user()

            meta = Util.read_metadata(filename)
            if not meta or not "sourceURL" in meta:
                return -2  # we only smart-report images coming from Variety online sources, not local images

            width, height = Util.get_size(filename)
            image = {
                'thumbnail': base64.b64encode(Util.get_thumbnail_data(filename, 300, 300)),
                'width': width,
                'height': height,
                'origin_url': meta['sourceURL'],
                'source_name': meta.get('sourceName', None),
                'source_location': meta.get('sourceLocation', None),
                'image_url': meta.get('imageURL', None)
            }

            logger.info("Smart-reporting %s as '%s'" % (filename, tag))
            try:
                url = Smart.API_URL + '/user/' + self.user['id'] + '/' + tag
                result = Util.fetch(url, {'image': json.dumps(image), 'authkey': self.user['authkey']})
                logger.info("Smart-reported, server returned: %s" % result)
                return 0
            except HTTPError, e:
                logger.error("Server returned %d, potential reason - server failure?" % e.code)
                if e.code in (403, 404):
                    self.parent.show_notification(
                        _('Your Smart Variety credentials are probably outdated. Please login again.'))
                    self.new_user()
                    self.parent.preferences_dialog.on_btn_login_register_clicked()

                if attempt == 3:
                    logger.exception(
                        "Could not smart-report %s as '%s, server error code %s'" % (filename, tag, e.code))
                    return -3
                return self.report_file(filename, tag, attempt + 1)
        except Exception:
            logger.exception("Could not smart-report %s as '%s'" % (filename, tag))
            return -4

    def show_notice_dialog(self):
        # Show Smart Variety notice
        dialog = SmartFeaturesNoticeDialog()

        def _on_ok(button):
            self.parent.options.smart_enabled = dialog.ui.enabled.get_active()
            self.parent.options.smart_notice_shown = True
            if self.parent.options.smart_enabled:
                for s in self.parent.options.sources:
                    if s[1] == Options.SourceType.RECOMMENDED:
                        self.parent.show_notification(_("Recommended source enabled"))
                        s[0] = True
            self.parent.options.write()
            self.parent.reload_config()
            dialog.destroy()
            self.parent.dialogs.remove(dialog)
            self.sync()

        dialog.ui.btn_ok.connect("clicked", _on_ok)
        self.parent.dialogs.append(dialog)
        dialog.run()

    def load_syncdb(self):
        logger.debug("sync: Loading syncdb")
        syncdb_file = os.path.join(self.parent.config_folder, 'syncdb.json')
        try:
            with open(syncdb_file) as f:
                syncdb = AttrDict(json.load(f, encoding='utf8'))
        except:
            syncdb = AttrDict()

        return syncdb

    def write_syncdb(self, syncdb):
        syncdb_file = os.path.join(self.parent.config_folder, 'syncdb.json')
        with open(syncdb_file, "w") as f:
            json.dump(syncdb.asdict(), f, encoding='utf8', indent=4, ensure_ascii=False)

    @staticmethod
    def get_image_id(url):
        return base64.urlsafe_b64encode(hashlib.md5(url).digest())[:10].replace('-', 'a').replace('_', 'b').lower()

    def is_smart_enabled(self):
        return self.parent.options.smart_notice_shown and self.parent.options.smart_enabled

    def is_sync_enabled(self):
        return self.is_smart_enabled() and \
               self.user is not None and self.user.get("username") is not None and \
               self.parent.options.sync_enabled

    def sync(self):
        if not self.is_smart_enabled():
            return

        self.sync_hash = Util.random_hash()
        current_sync_hash = self.sync_hash

        def _run():
            logger.info('sync: Started, hash %s' % current_sync_hash)

            try:
                self.load_user(True, True)

                logger.info("sync: Fetching serverside data")
                server_data = AttrDict(Util.fetch_json(Smart.API_URL + '/sync/' + self.user["id"]))

                syncdb = self.load_syncdb()

                # first upload local favorites that need uploading:
                logger.info("sync: Uploading local favorites to server")
                for name in os.listdir(self.parent.options.favorites_folder):
                    try:
                        if not self.is_smart_enabled() or current_sync_hash != self.sync_hash:
                            return

                        time.sleep(0.1)

                        path = os.path.join(self.parent.options.favorites_folder, name)
                        if not Util.is_image(path):
                            continue

                        if path in syncdb:
                            info = syncdb[path]
                        else:
                            info = {}
                            source_url = Util.get_variety_source_url(path)
                            if source_url:
                                info["sourceURL"] = source_url
                            syncdb[path] = info
                            self.write_syncdb(syncdb)

                        if not "sourceURL" in info:
                            continue

                        imageid = self.get_image_id(info["sourceURL"])
                        syncdb["id:" + imageid] = {"success": True}
                        self.write_syncdb(syncdb)

                        if not imageid in server_data["favorite"]:
                            logger.info("sync: Smart-reporting existing favorite %s" % path)
                            self.report_file(path, "favorite")
                            time.sleep(2)
                    except:
                        logger.exception("sync: Could not process file %s" % path)

                if not self.is_sync_enabled():
                    return

                # then download locally-missing favorites from the server
                to_sync = []
                for imageid in server_data["favorite"]:
                    if imageid in server_data["trash"]:
                        continue  # do not download favorites that have later been trashed;
                        # TODO: we need a better way to un-favorite things and forbid them from downloading

                    key = "id:" + imageid

                    if key in syncdb:
                        if 'success' in syncdb[key]:
                            continue  # we have this image locally
                        if syncdb[key].get('error', 0) >= 3:
                            continue  # we have tried and got error for this image 3 or more times, leave it alone
                    to_sync.append(imageid)

                if to_sync:
                    self.parent.show_notification(_("Sync"), _("Fetching %d images") % len(to_sync))

                for imageid in to_sync:
                    if not self.is_sync_enabled() or current_sync_hash != self.sync_hash:
                        return

                    key = "id:" + imageid
                    try:
                        logger.info("sync: Downloading locally-missing favorite image %s" % imageid)
                        image_data = Util.fetch_json(Smart.API_URL + '/image/' + imageid + '/json')

                        ImageFetcher.fetch(image_data["image_url"], self.parent.options.favorites_folder,
                                           source_url=image_data["origin_url"],
                                           source_name=image_data["sources"][0][0] if image_data.get("sources", []) else None,
                                           source_location=image_data["sources"][0][1] if image_data.get("sources", []) else None,
                                           verbose=False)
                        syncdb[key] = {"success": True}

                    except:
                        logger.exception("sync: Could not fetch favorite image %s" % imageid)
                        syncdb[key] = syncdb[key] or {}
                        syncdb[key].setdefault("error", 0)
                        syncdb[key]["error"] += 1

                    finally:
                        if not self.is_smart_enabled() or current_sync_hash != self.sync_hash:
                            return

                        self.write_syncdb(syncdb)
                        time.sleep(2)

                if to_sync:
                    self.parent.show_notification(_("Sync"), _("Finished"))
            finally:
                self.syncing = False

        sync_thread = threading.Thread(target=_run)
        sync_thread.daemon = True
        sync_thread.start()
