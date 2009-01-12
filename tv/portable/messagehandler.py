# Miro - an RSS based video player application
# Copyright (C) 2005-2008 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""messagehandler.py -- Backend message handler"""

from copy import copy
import logging
import time

from miro import app
from miro import config
from miro import database
from miro import eventloop
from miro import feed
from miro import filters
from miro.frontendstate import WidgetsFrontendState
from miro import guide
from miro import httpclient
from miro import indexes
from miro import messages
from miro import prefs
from miro import singleclick
from miro import subscription
from miro import views
from miro import opml
from miro import searchengines
from miro import util
from miro.feed import Feed, get_feed_by_url
from miro.gtcache import gettext as _
from miro.playlist import SavedPlaylist
from miro.folder import FolderBase, ChannelFolder, PlaylistFolder
from miro.util import getSingletonDDBObject
from miro.xhtmltools import urlencode

from miro.plat.utils import osFilenameToFilenameType, makeURLSafe

import shutil

class ViewTracker(object):
    """Handles tracking views for TrackGuides, TrackChannels, TrackPlaylist and TrackItems."""

    def __init__(self):
        self.add_callbacks()
        self.reset_changes()
        self.dont_send_messages = False
        self._last_sent_info = {}

    def reset_changes(self):
        self.changed = set()
        self.removed = set()
        self.added =  []
        # Need to use a list because added messages must be sent in the same
        # order they were receieved
        self.changes_pending = False

    def send_messages(self):
        # Try to reduce the number of messages we're sending out.
        self.changed -= self.removed
        self.changed -= set(self.added)

        for i in reversed(xrange(len(self.added))):
            if self.added[i] in self.removed:
                # Object was removed before we sent the add message, just
                # don't send any message
                self.removed.remove(self.added.pop(i))
        message = self.make_changed_message(
                self._make_added_list(self.added),
                self._make_changed_list(self.changed),
                self._make_removed_list(self.removed))
        if message.added or message.changed or message.removed:
            message.send_to_frontend()
        self.reset_changes()

    def _make_new_info(self, obj):
        info = self.InfoClass(obj)
        self._last_sent_info[obj.id] = copy(info)
        return info

    def _make_added_list(self, added):
        return [self._make_new_info(obj) for obj in added]

    def _make_changed_list(self, changed):
        retval = []
        for obj in changed:
            info = self.InfoClass(obj)
            if obj.id not in self._last_sent_info or info.__dict__ != self._last_sent_info[obj.id].__dict__:
                retval.append(info)
                self._last_sent_info[obj.id] = copy(info)
        return retval

    def _make_removed_list(self, removed):
        for obj in removed:
            del self._last_sent_info[obj.id]
        return [obj.id for obj in removed]


    def schedule_send_messages(self):
        # We don't send messages immediately so that if an object gets changed
        # multiple times, only one callback gets sent.
        if not self.changes_pending:
            eventloop.addUrgentCall(self.send_messages, 'view tracker update' )
            self.changes_pending = True

    def add_callbacks(self):
        for view in self.get_object_views():
            view.addAddCallback(self.on_object_added)
            view.addRemoveCallback(self.on_object_removed)
            view.add_change_callback(self.on_object_changed)

    def remove_callbacks(self):
        for view in self.get_object_views():
            view.removeAddCallback(self.on_object_added)
            view.removeRemoveCallback(self.on_object_removed)
            view.remove_change_callback(self.on_object_changed)

    def on_object_added(self, obj, id):
        if self.dont_send_messages:
            # even though we're not sending messages, call _make_new_info() to
            # update _last_sent_info
            self._make_new_info(obj)
            return
        if obj in self.removed:
            # object was already removed, we need to send that message out
            # before we send the add message.
            self.send_messages()
        self.added.append(obj)
        self.schedule_send_messages()

    def on_object_removed(self, obj, id):
        if self.dont_send_messages:
            # even though we're not sending messages, update _last_sent_info
            del self._last_sent_info[id]
            return
        self.removed.add(obj)
        self.schedule_send_messages()

    def on_object_changed(self, obj, id):
        if self.dont_send_messages:
            # even though we're not sending messages, call _make_new_info() to
            # update _last_sent_info
            self._make_new_info(obj)
            return
        self.changed.add(obj)
        self.schedule_send_messages()

    def unlink(self):
        self.remove_callbacks()

class TabTracker(ViewTracker):
    def __init__(self):
        ViewTracker.__init__(self)
        self.send_whole_list = False

    def make_changed_message(self, added, changed, removed):
        return messages.TabsChanged(self.type, added, changed, removed)

    def send_messages(self):
        if self.send_whole_list:
            self.send_initial_list()
            self.send_whole_list = False
            self.reset_changes()
        else:
            ViewTracker.send_messages(self)

    def add_callbacks(self):
        for view in self.get_object_views():
            view.addAddCallback(self.on_object_added)
            view.addRemoveCallback(self.on_object_removed)
            view.add_change_callback(self.on_object_changed)

    def remove_callbacks(self):
        for view in self.get_object_views():
            view.removeAddCallback(self.on_object_added)
            view.removeRemoveCallback(self.on_object_removed)
            view.remove_change_callback(self.on_object_changed)

    def send_initial_list(self):
        response = messages.TabList(self.type)
        current_folder_id = None
        for tab in self.get_tab_view():
            info = self._make_new_info(tab.obj)
            if tab.obj.getFolder() is None:
                response.append(info)
                if isinstance(tab.obj, FolderBase):
                    current_folder_id = tab.objID()
                    if tab.obj.getExpanded():
                        response.expand_folder(tab.objID())
                else:
                    current_folder_id = None
            else:
                if (current_folder_id is None or
                        tab.obj.getFolder().id != current_folder_id):
                    raise AssertionError("Tab ordering is wrong")
                response.append_child(current_folder_id, info)
        response.send_to_frontend()

class ChannelTracker(TabTracker):
    type = 'feed'
    InfoClass = messages.ChannelInfo

    def get_object_views(self):
        return views.videoVisibleFeeds, views.videoChannelFolders

    def get_tab_view(self):
        return getSingletonDDBObject(views.channelTabOrder).getView()

class AudioChannelTracker(TabTracker):
    type = 'audio-feed'
    InfoClass = messages.ChannelInfo

    def get_object_views(self):
        return views.audioVisibleFeeds, views.audioChannelFolders

    def get_tab_view(self):
        return getSingletonDDBObject(views.audioChannelTabOrder).getView()

class PlaylistTracker(TabTracker):
    type = 'playlist'
    InfoClass = messages.PlaylistInfo

    def get_object_views(self):
        return views.playlists, views.playlistFolders

    def get_tab_view(self):
        return getSingletonDDBObject(views.playlistTabOrder).getView()

class GuideTracker(ViewTracker):
    InfoClass = messages.GuideInfo

    def get_object_views(self):
        return [views.guides]

    def make_changed_message(self, added, changed, removed):
        return messages.TabsChanged('guide', added, changed, removed)

    def send_initial_list(self):
        info_list = self._make_added_list(views.guides)
        messages.GuideList(info_list).send_to_frontend()

class WatchedFolderTracker(ViewTracker):
    InfoClass = messages.WatchedFolderInfo

    def get_object_views(self):
        return [views.watchedFolders]

    def make_changed_message(self, added, changed, removed):
        return messages.WatchedFoldersChanged(added, changed, removed)

    def send_initial_list(self):
        info_list = self._make_added_list(views.watchedFolders)
        messages.WatchedFolderList(info_list).send_to_frontend()

class ItemTrackerBase(ViewTracker):
    InfoClass = messages.ItemInfo

    def make_changed_message(self, added, changed, removed):
        return messages.ItemsChanged(self.type, self.id,
                added, changed, removed)

    def get_object_views(self):
        return [self.view]

    def send_initial_list(self):
        infos = self._make_added_list(self.view)
        messages.ItemList(self.type, self.id, infos).send_to_frontend()

class FeedItemTracker(ItemTrackerBase):
    type = 'feed'
    def __init__(self, feed):
        self.view = feed.items
        self.id = feed.id
        ItemTrackerBase.__init__(self)

class FeedFolderItemTracker(ItemTrackerBase):
    type = 'feed'
    def __init__(self, folder):
        self.view = views.items.filterWithIndex(
            indexes.itemsByChannelFolder,
            folder).filter(filters.uniqueItems)
        self.id = folder.id
        ItemTrackerBase.__init__(self)

    def unlink(self):
        ItemTrackerBase.unlink(self)
        self.view.unlink()

class PlaylistItemTracker(ItemTrackerBase):
    type = 'playlist'
    def __init__(self, playlist):
        self.view = playlist.trackedItems.view
        self.id = playlist.id
        ItemTrackerBase.__init__(self)

class ManualItemTracker(ItemTrackerBase):
    type = 'manual'

    def __init__(self, id, id_list):
        self.id = id
        self.id_set = set(id_list)
        self.view = views.items.filter(self.filter)
        ItemTrackerBase.__init__(self)

    def filter(self, obj):
        return obj.id in self.id_set

    def unlink(self):
        ItemTrackerBase.unlink(self)
        self.view.unlink()

class DownloadingItemsTracker(ItemTrackerBase):
    type = 'downloads'
    id = None
    def __init__(self):
        self.view = views.allDownloadingItems.filter(filters.uniqueItems)
        ItemTrackerBase.__init__(self)

    def unlink(self):
        ItemTrackerBase.unlink(self)
        self.view.unlink()

class IndividualDownloadsTracker(ItemTrackerBase):
    type = 'individual_downloads'
    id = None
    def __init__(self):
        self.view = views.individualItems
        ItemTrackerBase.__init__(self)

class NewItemsTracker(ItemTrackerBase):
    type = 'new'
    id = None
    def __init__(self):
        self.view = views.uniqueNewWatchableItems
        ItemTrackerBase.__init__(self)

class LibraryItemsTracker(ItemTrackerBase):
    type = 'library'
    id = None
    def __init__(self):
        self.view = views.uniqueWatchableItems
        ItemTrackerBase.__init__(self)

class SearchItemsTracker(ItemTrackerBase):
    type = 'search'
    id = None
    def __init__(self):
        self.view = views.searchItems
        ItemTrackerBase.__init__(self)

def make_item_tracker(message):
    if message.type == 'downloads':
        return DownloadingItemsTracker()
    elif message.type == 'individual_downloads':
        return IndividualDownloadsTracker()
    elif message.type == 'new':
        return NewItemsTracker()
    elif message.type == 'library':
        return LibraryItemsTracker()
    elif message.type == 'search':
        return SearchItemsTracker()
    elif message.type == 'feed':
        try:
            feed = views.feeds.getObjectByID(message.id)
            return FeedItemTracker(feed)
        except database.ObjectNotFoundError:
            folder = views.channelFolders.getObjectByID(message.id)
            return FeedFolderItemTracker(folder)
    elif message.type == 'playlist':
        try:
            playlist = views.playlists.getObjectByID(message.id)
            return PlaylistItemTracker(playlist)
        except database.ObjectNotFoundError:
            playlist = views.playlistFolders.getObjectByID(message.id)
            return PlaylistItemTracker(playlist)
    elif message.type == 'manual':
        return ManualItemTracker(message.id, message.ids_to_track)
    else:
        logging.warn("Unknown TrackItems type: %s", message.type)

class CountTracker(object):
    """Tracks downloads count or new videos count"""
    def __init__(self):
        self.view = self.get_view()
        self.view.addAddCallback(self.on_count_changed)
        self.view.addRemoveCallback(self.on_count_changed)

    def on_count_changed(self, obj, id):
        self.send_message()

    def send_message(self):
        self.make_message(len(self.view)).send_to_frontend()

    def stop_tracking(self):
        self.view.removeAddCallback(self.on_count_changed)
        self.view.removeRemoveCallback(self.on_count_changed)

class DownloadCountTracker(CountTracker):
    def get_view(self):
        return views.downloadingItems

    def make_message(self, count):
        return messages.DownloadCountChanged(count)

class PausedCountTracker(CountTracker):
    def get_view(self):
        return views.pausedItems

    def make_message(self, count):
        return messages.PausedCountChanged(count)

class NewCountTracker(CountTracker):
    def get_view(self):
        return views.uniqueNewWatchableItems

    def make_message(self, count):
        return messages.NewCountChanged(count)

class UnwatchedCountTracker(CountTracker):
    def get_view(self):
        return views.unwatchedItems

    def make_message(self, count):
        return messages.UnwatchedCountChanged(count)

class BackendMessageHandler(messages.MessageHandler):
    def __init__(self, frontend_startup_callback):
        messages.MessageHandler.__init__(self)
        self.frontend_startup_callback = frontend_startup_callback
        self.channel_tracker = None
        self.audio_channel_tracker = None
        self.playlist_tracker = None
        self.guide_tracker = None
        self.watched_folder_tracker = None
        self.download_count_tracker = None
        self.paused_count_tracker = None
        self.new_count_tracker = None
        self.unwatched_count_tracker = None
        self.item_trackers = {}
        search_feed = app.controller.get_global_feed('dtv:search')
        search_feed.connect('update-finished', self._search_update_finished)

    def call_handler(self, method, message):
        name = 'handling backend message: %s' % message
        logging.debug("handling backend %s", message)
        eventloop.addUrgentCall(method, name, args=(message,))

    def folder_view_for_type(self, type):
        if type == 'feed':
            return views.videoChannelFolders
        elif type == 'audio-feed':
            return views.audioChannelFolders
        elif type == 'playlist':
            return views.playlistFolders
        else:
            raise ValueError("Unknown Type: %s" % type)

    def view_for_type(self, type):
        if type == 'feed':
            return views.videoVisibleFeeds
        elif type == 'audio-feed':
            return views.audioVisibleFeeds
        elif type == 'playlist':
            return views.playlists
        elif type == 'feed-folder':
            return views.channelFolders
        elif type == 'playlist-folder':
            return views.playlistFolders
        elif type == 'site':
            return views.guides
        else:
            raise ValueError("Unknown Type: %s" % type)

    def handle_frontend_started(self, message):
        # add a little bit more delay to let things simmer down a bit.  The
        # calls here are low-priority, so we can afford to wait a bit.
        eventloop.addTimeout(2, self.frontend_startup_callback,
                'frontend startup callback')

    def handle_query_search_info(self, message):
        search_feed = app.controller.get_global_feed('dtv:search')
        messages.CurrentSearchInfo(search_feed.lastEngine,
                search_feed.lastQuery).send_to_frontend()

    def handle_track_channels(self, message):
        if not self.channel_tracker:
            self.channel_tracker = ChannelTracker()
        if not self.audio_channel_tracker:
            self.audio_channel_tracker = AudioChannelTracker()
        self.channel_tracker.send_initial_list()
        self.audio_channel_tracker.send_initial_list()

    def handle_stop_tracking_channels(self, message):
        if self.channel_tracker:
            self.channel_tracker.unlink()
            self.channel_tracker = None

    def handle_track_guides(self, message):
        if not self.guide_tracker:
            self.guide_tracker = GuideTracker()
        self.guide_tracker.send_initial_list()

    def handle_stop_tracking_guides(self, message):
        if self.guide_tracker:
            self.guide_tracker.unlink()
            self.guide_tracker = None

    def handle_track_watched_folders(self, message):
        if not self.watched_folder_tracker:
            self.watched_folder_tracker = WatchedFolderTracker()
        self.watched_folder_tracker.send_initial_list()

    def handle_stop_tracking_watched_folders(self, message):
        if self.watched_folder_tracker:
            self.watched_folder_tracker.unlink()
            self.watched_folder_tracker = None

    def handle_track_playlists(self, message):
        if not self.playlist_tracker:
            self.playlist_tracker = PlaylistTracker()
        self.playlist_tracker.send_initial_list()

    def handle_stop_tracking_playlists(self, message):
        if self.playlist_tracker:
            self.playlist_tracker.unlink()
            self.playlist_tracker = None

    def handle_mark_feed_seen(self, message):
        try:
            feed = database.defaultDatabase.getObjectByID(message.id)
            feed.markAsViewed()
        except database.ObjectNotFoundError:
            logging.warning("handle_mark_feed_seen: can't find feed by id %s", message.id)

    def handle_mark_item_watched(self, message):
        try:
            item = views.items.getObjectByID(message.id)
            item.markItemSeen()
        except database.ObjectNotFoundError:
            logging.warning("handle_mark_item_seen: can't find item by id %s", message.id)

    def handle_mark_item_unwatched(self, message):
        try:
            item = views.items.getObjectByID(message.id)
            item.markItemUnseen()
        except database.ObjectNotFoundError:
            logging.warning("handle_mark_item_unwatched: can't find item by id %s", message.id)

    def handle_set_item_resume_time(self, message):
        try:
            item = views.items.getObjectByID(message.id)
            item.setResumeTime(message.resume_time)
        except database.ObjectNotFoundError:
            logging.warning("handle_set_item_resume_time: can't find item by id %s", message.id)

    def handle_set_feed_expire(self, message):
        channel_info = message.channel_info
        expire_type = message.expire_type
        expire_time = message.expire_time

        try:
            channel = views.feeds.getObjectByID(channel_info.id)
            if expire_type == "never":
                channel.setExpiration(u"never", 0)
            elif expire_type == "system":
                channel.setExpiration(u"system", expire_time)
            else:
                channel.setExpiration(u"feed", expire_time)

        except database.ObjectNotFoundError:
            logging.warning("handle_set_feed_expire: can't find feed by id %s", channel_info.id)

    def handle_set_feed_max_new(self, message):
        channel_info = message.channel_info
        value = message.max_new

        try:
            channel = views.feeds.getObjectByID(channel_info.id)
            if value == u"unlimited":
                channel.set_max_new(-1)
            else:
                channel.set_max_new(value)

        except database.ObjectNotFoundError:
            logging.warning("handle_set_feed_max_new: can't find feed by id %s", channel_info.id)

    def handle_set_feed_max_old_items(self, message):
        channel_info = message.channel_info
        max_old_items = message.max_old_items

        try:
            channel = views.feeds.getObjectByID(channel_info.id)
            channel.setMaxOldItems(max_old_items)

        except database.ObjectNotFoundError:
            logging.warning("handle_set_feed_max_new: can't find feed by id %s", channel_info.id)

    def handle_clean_feed(self, message):
        channel_id = message.channel_id
        try:
            obj = views.feeds.getObjectByID(channel_id)
        except database.ObjectNotFoundError:
            logging.warn("handle_clean_feed: object not found id: %s" % channel_id)
        else:
            obj.clean_old_items()

    def handle_import_feeds(self, message):
        opml.Importer().import_subscriptions(message.filename)

    def handle_export_feeds(self, message):
        opml.Exporter().export_subscriptions(message.filename)

    def handle_rename_object(self, message):
        view = self.view_for_type(message.type)
        try:
            obj = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("object not found (type: %s, id: %s)" %
                    (message.type, message.id))
        else:
            obj.setTitle(message.new_name)

    def handle_play_all_unwatched(self, message):
        item_infos = [messages.ItemInfo(i) for i in views.unwatchedItems]
        messages.PlayMovie(item_infos).send_to_frontend()

    def handle_folder_expanded_change(self, message):
        folder_view = self.folder_view_for_type(message.type)
        try:
            folder = folder_view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("feed folder not found")
        else:
            folder.setExpanded(message.expanded)

    def handle_update_feed(self, message):
        view = views.visibleFeeds
        try:
            feed = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("feed not found: %s" % id)
        else:
            feed.update()

    def handle_update_feed_folder(self, message):
        view = views.channelFolders
        try:
            f = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("folder not found: %s" % id)
        else:
            view = f.getChildrenView()
            for feed in view:
                feed.update()

    def handle_update_all_feeds(self, message):
        for f in views.feeds:
            f.scheduleUpdateEvents(0)

    def handle_delete_feed(self, message):
        if message.is_folder:
            view = views.channelFolders
        else:
            view = views.visibleFeeds
        try:
            channel = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("feed not found: %s" % message.id)
        else:
            if message.keep_items:
                move_to = getSingletonDDBObject(views.manualFeed)
            else:
                move_to = None
            channel.remove(move_to)

    def handle_delete_watched_folder(self, message):
        try:
            channel = views.watchedFolders.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("watched folder not found: %s" % message.id)
        else:
            channel.remove()

    def handle_delete_playlist(self, message):
        if message.is_folder:
            view = views.playlistFolders
        else:
            view = views.playlists
        try:
            playlist = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("playlist not found: %s" % message.id)
        else:
            playlist.remove()

    def handle_delete_site(self, message):
        site = views.guides.getObjectByID(message.id)
        if site.getDefault():
            raise ValueError("Can't delete default site")
        site.remove()

    def handle_tabs_reordered(self, message):
        # The frontend already has the channels in the correct order and with
        # the correct parents.  Don't send it updates based on the backend
        # re-aranging things
        if self.channel_tracker:
            self.channel_tracker.dont_send_messages = True
        if self.audio_channel_tracker:
            self.audio_channel_tracker.dont_send_messages = True
        try:
            self._do_handle_tabs_reordered(message)
        finally:
            if self.channel_tracker:
                self.channel_tracker.dont_send_messages = False
            if self.audio_channel_tracker:
                self.audio_channel_tracker.dont_send_messages = False

    def _do_handle_tabs_reordered(self, message):
        video_order = getSingletonDDBObject(views.channelTabOrder)
        audio_order = getSingletonDDBObject(views.audioChannelTabOrder)
        playlist_order = getSingletonDDBObject(views.playlistTabOrder)

        # make sure all the items are in the right places
        for info in message.toplevels['feed']:
            item = views.allTabs.getObjectByID(info.id)
            if item.type != u'feed' or item.obj.section != u'video':
                item.type = u'feed'
                item.obj.section = u'video'
                item.signalChange()
                item.obj.signalChange()

        for info in message.toplevels['audio-feed']:
            item = views.allTabs.getObjectByID(info.id)
            if item.type != u'audio-feed' or item.obj.section != u'audio':
                item.type = u'audio-feed'
                item.obj.section = u'audio'
                item.signalChange()
                item.obj.signalChange()

        for id_, feeds in message.folder_children.iteritems():
            feed_folder = views.allTabs.getObjectByID(id_)
            for mem in feeds:
                mem = views.allTabs.getObjectByID(mem.id)
                if feed_folder.type == u'audio-feed' and mem.type != u'audio-feed':
                    mem.type = u'audio-feed'
                    mem.obj.section = u'audio'
                    mem.signalChange()
                    mem.obj.signalChange()
                elif feed_folder.type == u'feed' and mem.type != u'feed':
                    mem.type = u'feed'
                    mem.obj.section = u'video'
                    mem.signalChange()
                    mem.obj.signalChange()

        for info_type, info_list in message.toplevels.iteritems():
            folder_view = self.folder_view_for_type(info_type)

            if info_type == 'feed':
                item_view = views.visibleFeeds
                tab_order = video_order
            elif info_type == 'audio-feed':
                item_view = views.visibleFeeds
                tab_order = audio_order
            elif info_type == 'playlist':
                item_view = views.playlists
                tab_order = playlist_order
            else:
                raise ValueError("Unknown Type: %s" % message.type)

            order = []
            for info in info_list:
                order.append(info.id)
                if info.is_folder:
                    folder = folder_view.getObjectByID(info.id)
                    for child_info in message.folder_children[info.id]:
                        child_id = child_info.id
                        order.append(child_id)
                        feed = item_view.getObjectByID(child_id)
                        feed.setFolder(folder)
                else:
                    feed = item_view.getObjectByID(info.id)
                    feed.setFolder(None)
            tab_order.reorder(order)
            tab_order.signalChange()

    def handle_playlist_reordered(self, message):
        try:
            playlist = views.playlists.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            try:
                playlist = views.playlistFolders.getObjectByID(message.id)
            except database.ObjectNotFoundError:
                logging.warn("PlaylistReordered: Playlist not found -- %s",
                        message.id)
                return

        if set(playlist.item_ids) != set(message.item_ids):
            logging.warn("PlaylistReordered: Not all ids present in the new order\nOriginal Ids: %s\nNew ids: %s", playlist.item_ids, message.item_ids)
            return
        playlist.reorder(message.item_ids)
        playlist.signalChange()

    def handle_new_guide(self, message):
        url = message.url
        if guide.getGuideByURL(url) is None:
            guide.ChannelGuide(url, [u'*'])

    def handle_new_feed(self, message):
        url = message.url
        if not get_feed_by_url(url):
            Feed(url, section=message.section)

    def handle_new_feed_search_feed(self, message):
        term = message.search_term
        channel_info = message.channel_info
        section = message.section
        location = channel_info.base_href

        if isinstance(term, unicode):
            term = term.encode("utf-8")

        if isinstance(location, unicode):
            location = location.encode("utf-8")

        if channel_info.search_term:
            term = term + " " + channel_info.search_term

        url = u"dtv:searchTerm:%s?%s" % (urlencode(location), urlencode(term))
        if not get_feed_by_url(url):
            Feed(url, section=section)

    def handle_new_feed_search_engine(self, message):
        sei = message.search_engine_info
        term = message.search_term
        section = message.section

        url = searchengines.get_request_url(sei.name, term)

        if not url:
            return

        if not get_feed_by_url(url):
            f = Feed(url, section=section)

    def handle_new_feed_search_url(self, message):
        url = message.url
        term = message.search_term
        section = message.section

        if isinstance(term, unicode):
            term = term.encode("utf-8")

        normalized = feed.normalize_feed_url(url)

        if isinstance(url, unicode):
            url = url.encode("utf-8")

        url = u"dtv:searchTerm:%s?%s" % (urlencode(normalized), urlencode(term))
        if not get_feed_by_url(url):
            Feed(url, section=section)

    def handle_new_feed_folder(self, message):
        folder = ChannelFolder(message.name, message.section)

        if message.child_feed_ids is not None:
            section = message.section
            for id in message.child_feed_ids:
                feed_ = views.feeds.getObjectByID(id)
                feed_.setFolder(folder)
                if feed_.section != section:
                    feed_tab = views.allTabs.getObjectByID(feed_.id)
                    if section == u'video':
                        feed_tab.type = u'feed'
                        feed_.section = u'video'
                    else:
                        feed_tab.type = u'audio-feed'
                        feed_.section = u'audio'
                    feed_tab.signalChange()
                    feed_.signalChange()
            if section == u'video':
                tab_order = getSingletonDDBObject(views.channelTabOrder)
                tracker = self.channel_tracker
            else:
                tab_order = getSingletonDDBObject(views.audioChannelTabOrder)
                tracker = self.audio_channel_tracker
            tab_order.move_tab_after(folder.id, message.child_feed_ids)
            tab_order.signalChange()
            tracker.send_whole_list = True

    def handle_new_watched_folder(self, message):
        path = osFilenameToFilenameType(message.path)
        url = u"dtv:directoryfeed:%s" % makeURLSafe(path)
        if not get_feed_by_url(url):
            feed.Feed(url)
        else:
            logging.info("Not adding dupplicated watched folder: %s",
                    message.path)

    def handle_set_watched_folder_visible(self, message):
        feed = views.feeds.getObjectByID(message.id)
        if not feed.url.startswith("dtv:directoryfeed:"):
            raise ValueError("%s is not a watched folder" % feed)
        feed.setVisible(message.visible)

    def handle_new_playlist(self, message):
        name = message.name
        ids = message.ids
        if not ids:
            ids = None
        SavedPlaylist(name, ids)

    def handle_download_url(self, message):
        singleclick.add_download(message.url)

    def handle_open_individual_file(self, message):
        fn = osFilenameToFilenameType(message.filename)
        singleclick.parse_command_line_args([fn])

    def handle_open_individual_files(self, message):
        fns = [osFilenameToFilenameType(fn) for fn in message.filenames]
        singleclick.parse_command_line_args(fns)

    def handle_check_version(self, message):
        up_to_date_callback = message.up_to_date_callback
        from miro import autoupdate
        autoupdate.check_for_updates(up_to_date_callback)

    def handle_new_playlist_folder(self, message):
        folder = PlaylistFolder(message.name)
        if message.child_playlist_ids is not None:
            for id in message.child_playlist_ids:
                playlist = views.playlists.getObjectByID(id)
                playlist.setFolder(folder)
            tab_order = getSingletonDDBObject(views.playlistTabOrder)
            tab_order.move_tab_after(folder.id, message.child_playlist_ids)
            tab_order.signalChange()
            self.playlist_tracker.send_whole_list = True

    def handle_add_videos_to_playlist(self, message):
        try:
            playlist = views.playlists.getObjectByID(message.playlist_id)
        except database.ObjectNotFoundError:
            logging.warn("AddVideosToPlaylist: Playlist not found -- %s",
                    message.playlist_id)
            return
        for id in message.video_ids:
            try:
                item = views.items.getObjectByID(id)
            except database.ObjectNotFoundError:
                logging.warn("AddVideosToPlaylist: Item not found -- %s", id)
                continue
            if not item.is_downloaded():
                logging.warn("AddVideosToPlaylist: Item not downloaded (%s)",
                        item)
            else:
                playlist.addItem(item)

    def handle_remove_videos_from_playlist(self, message):
        try:
            playlist = views.playlists.getObjectByID(message.playlist_id)
        except database.ObjectNotFoundError:
            logging.warn("RemoveVideosFromPlaylist: Playlist not found -- %s",
                    message.playlist_id)
            return
        to_remove = []
        for id in message.video_ids:
            if not playlist.idInPlaylist(id):
                logging.warn("RemoveVideosFromPlaylist: Id not found -- %s",
                        id)
            else:
                to_remove.append(id)
        if to_remove:
            playlist.handleRemove(to_remove)

    def handle_search(self, message):
        searchengine_id = message.id
        terms = message.terms

        search_feed = app.controller.get_global_feed('dtv:search')
        search_downloads_feed = app.controller.get_global_feed('dtv:searchDownloads')

        search_feed.preserveDownloads(search_downloads_feed)
        if terms:
            search_feed.lookup(searchengine_id, terms)
        else:
            search_feed.set_info(searchengine_id, u'')
            search_feed.reset()

    def _search_update_finished(self, feed):
        messages.SearchComplete(feed.lastEngine, feed.lastQuery,
                len(feed.items)).send_to_frontend()

    def item_tracker_key(self, message):
        if message.type != 'manual':
            return (message.type, message.id)
        else:
            # make sure the item list is a tuple, so it can be hashed.
            return (message.type, tuple(message.id))

    def handle_track_items(self, message):
        key = self.item_tracker_key(message)
        if key not in self.item_trackers:
            try:
                item_tracker = make_item_tracker(message)
            except database.ObjectNotFoundError:
                logging.warn("TrackItems called for deleted object (%s %s)",
                        message.type, message.id)
                return
            if item_tracker is None:
                # message type was wrong
                return
            self.item_trackers[key] = item_tracker
        else:
            item_tracker = self.item_trackers[key]
        item_tracker.send_initial_list()

    def handle_track_items_manually(self, message):
        # handle_track_items can handle this message too
        self.handle_track_items(message)

    def handle_stop_tracking_items(self, message):
        key = self.item_tracker_key(message)
        try:
            item_tracker = self.item_trackers.pop(key)
        except KeyError:
            logging.warn("Item tracker not found (id: %s)", message.id)
        else:
            item_tracker.unlink()

    def handle_start_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("StartDownload: Item not found -- %s", message.id)
        else:
            item.download()

    def handle_cancel_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("CancelDownload: Item not found -- %s", message.id)
        else:
            item.expire()

    def handle_pause_all_downloads(self, message):
        """Pauses all downloading and uploading items"""
        for item in views.downloadingItems:
            item.pause()

        for item in views.allDownloadingItems:
            if item.is_uploading():
                item.pauseUpload()

    def handle_pause_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("PauseDownload: Item not found -- %s", message.id)
        else:
            item.pause()

    def handle_resume_all_downloads(self, message):
        """Resumes downloading and uploading items"""
        for item in views.pausedItems:
            item.resume()

        for item in views.allDownloadingItems:
            if item.is_uploading_paused():
                item.startUpload()

    def handle_resume_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("ResumeDownload: Item not found -- %s", message.id)
        else:
            item.resume()

    def handle_cancel_all_downloads(self, message):
        for item in views.pausedItems:
            if item.is_uploading() or item.is_uploading_paused():
                item.stopUpload()
            else:
                item.expire()

        for item in views.downloadingItems:
            item.expire()

        for item in views.allDownloadingItems:
            if item.is_uploading() or item.is_uploading_paused():
                item.stopUpload()

    def handle_start_upload(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("handle_start_upload: Item not found -- %s", message.id)
        else:
            if item.downloader.getType() != 'bittorrent':
                logging.warn("%s is not a torrent", item)
            elif item.is_uploading():
                logging.warn("%s is already uploading", item)
            else:
                item.startUpload()

    def handle_stop_upload(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("handle_stop_upload: Item not found -- %s", message.id)
        else:
            if item.downloader.getType() != 'bittorrent':
                logging.warn("%s is not a torrent", item)
            elif not item.is_uploading():
                logging.warn("%s is already stopped", item)
            else:
                item.stopUpload()

    def handle_keep_video(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("KeepVideo: Item not found -- %s", message.id)
        else:
            item.save()

    def handle_save_item_as(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("SaveVideoAs: Item not found -- %s", message.id)
            return

        logging.info("saving video %s to %s" % (item.get_video_filename(),
                                                message.filename))
        try:
            shutil.copyfile(item.get_video_filename(), message.filename)
        except IOError:
            # FIXME - we should pass the error back to the frontend
            pass

    def handle_add_item_to_library(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("AddItemToLibrary: Item not found -- %s", message.id)
            return

        item.add_to_library()
        manualFeed = getSingletonDDBObject(views.manualFeed)
        changed = set()
        changed.add(messages.ItemInfo(item))
        # I think we have to do this manually because it's in the manualFeed.
        messages.ItemsChanged('feed', manualFeed.getID(), [], changed, set()).send_to_frontend()

    def handle_remove_video_entry(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("RemoveVideoEntry: Item not found -- %s", message.id)
        else:
            item.expire()

    def handle_delete_video(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("DeleteVideo: Item not found -- %s", message.id)
        else:
            item.delete_files()
            item.expire()

    def handle_rename_video(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("RenameVideo: Item not found -- %s", message.id)
        else:
            item.setTitle(message.new_name)

    def handle_revert_feed_title(self, message):
        try:
            feed = views.feeds.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("RevertFeedTitle: Feed not found -- %s", message.id)
        else:
            feed.revert_title()

    def handle_revert_item_title(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("RevertItemTitle: Item not found -- %s", message.id)
        else:
            item.revert_title()

    def handle_autodownload_change(self, message):
        try:
            feed = views.feeds.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("AutodownloadChange: Feed not found -- %s", message.id)
        else:
            feed.setAutoDownloadMode(message.setting)

    def handle_track_download_count(self, message):
        if self.download_count_tracker is None:
            self.download_count_tracker = DownloadCountTracker()
        self.download_count_tracker.send_message()

    def handle_stop_tracking_download_count(self, message):
        if self.download_count_tracker:
            self.download_count_tracker.stop_tracking()
            self.download_count_tracker = None

    def handle_track_paused_count(self, message):
        if self.paused_count_tracker is None:
            self.paused_count_tracker = PausedCountTracker()
        self.paused_count_tracker.send_message()

    def handle_stop_tracking_paused_count(self, message):
        if self.paused_count_tracker:
            self.paused_count_tracker.stop_tracking()
            self.paused_count_tracker = None

    def handle_track_new_count(self, message):
        if self.new_count_tracker is None:
            self.new_count_tracker = NewCountTracker()
        self.new_count_tracker.send_message()

    def handle_stop_tracking_new_count(self, message):
        if self.new_count_tracker:
            self.new_count_tracker.stop_tracking()
            self.new_count_tracker = None

    def handle_track_unwatched_count(self, message):
        if self.unwatched_count_tracker is None:
            self.unwatched_count_tracker = UnwatchedCountTracker()
        self.unwatched_count_tracker.send_message()

    def handle_stop_tracking_unwatched_count(self, message):
        if self.unwatched_count_tracker:
            self.unwatched_count_tracker.stop_tracking()
            self.unwatched_count_tracker = None

    def handle_subscription_link_clicked(self, message):
        url = message.url
        type, subscribeURLs = subscription.find_subscribe_links(url)
        normalizedURLs = []
        for url, additional in subscribeURLs:
            normalized = feed.normalize_feed_url(url)
            if feed.validate_feed_url(normalized) and not feed.get_feed_by_url(normalized):
                normalizedURLs.append((normalized, additional))
        if normalizedURLs:
            if type == 'feed':
                feed_names = []
                for url, additional in normalizedURLs:
                    new_feed = feed.Feed(url, section=additional.get("section", u"video"))
                    feed_names.append(new_feed.get_title())
                    if 'trackback' in additional:
                        httpclient.grabURL(additional['trackback'],
                                           lambda x: None,
                                           lambda x: None)

                # send a notification to the user
                if len(feed_names) == 1:
                    title = _("Subscribed to new feed:")
                    body = feed_names[0]
                else:
                    title = _('Subscribed to new feeds:')
                    body = '\n'.join(
                        [' - %s' % feed_name for feed_name in feed_names])

                messages.NotifyUser(
                    title, body, 'feed-subscribe').send_to_frontend()
            elif type == 'download':
                for url, additional in normalizedURLs:
                    singleclick.download_video_url(url, additional)
            elif type == 'site':
                for url, additional in normalizedURLs:
                    if guide.getGuideByURL (url) is None:
                        guide.ChannelGuide(url, [u'*'])
            else:
                raise AssertionError("Unknown subscribe type")

    def handle_change_movies_directory(self, message):
        old_dir = config.get(prefs.MOVIES_DIRECTORY)
        config.set(prefs.MOVIES_DIRECTORY, message.path)
        if message.migrate:
            self._migrate(message.path)
        util.getSingletonDDBObject(views.directoryFeed).update()

    def _migrate(self, new_path):
        to_migrate = [d for d in views.remoteDownloads if d.isFinished()]
        migration_count = len(to_migrate)
        last_progress_time = 0
        for i, download in enumerate(to_migrate):
            current_time = time.time()
            if current_time > last_progress_time + 0.5:
                m = messages.MigrationProgress(i, migration_count, False)
                m.send_to_frontend()
                last_progress_time = current_time
            logging.info("migrating %s", download.get_filename())
            download.migrate(new_path)
        # Pass in case they don't exist or are not empty:
        try:
            fileutil.rmdir(os.path.join(old_dir, 'Incomplete Downloads'))
        except (SystemExit, KeyboardInterrupt):
            raise
        except:
            pass
        try:
            fileutil.rmdir(old_dir)
        except (SystemExit, KeyboardInterrupt):
            raise
        except:
            pass
        m = messages.MigrationProgress(migration_count, migration_count, True)
        m.send_to_frontend()


    def handle_report_crash(self, message):
        app.controller.sendBugReport(message.report, message.text, message.send_report)

    def handle_save_frontend_state(self, message):
        view = app.db.filterWithIndex(indexes.objectsByClass,
                WidgetsFrontendState)
        try:
            state = getSingletonDDBObject(view)
        except LookupError:
            state = WidgetsFrontendState()
        state.list_view_displays = message.list_view_displays
        state.signalChange()

    def handle_query_frontend_state(self, message):
        view = app.db.filterWithIndex(indexes.objectsByClass,
                WidgetsFrontendState)
        try:
            state = getSingletonDDBObject(view)
        except LookupError:
            state = WidgetsFrontendState()
        messages.CurrentFrontendState(state.list_view_displays).send_to_frontend()
