# Miro - an RSS based video player application
# Copyright (C) 2005-2007 Participatory Culture Foundation
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

import app
import traceback
import gobject
import eventloop
import config
import prefs
import os
import logging
from download_utils import nextFreeFilename
from platformutils import confirmMainThread
from gtk_queue import gtkAsyncMethod, gtkSyncMethod

from threading import Event

import pygtk
import gtk

import pygst
pygst.require('0.10')
import gst
import gst.interfaces

class Tester:
    def __init__(self, filename):
        self.done = Event()
        self.success = False
        self.actualInit(filename)

    @gtkSyncMethod
    def actualInit(self, filename):
        confirmMainThread()
        self.playbin = gst.element_factory_make('playbin')
        self.videosink = gst.element_factory_make("fakesink", "videosink")
        self.playbin.set_property("video-sink", self.videosink)
        self.audiosink = gst.element_factory_make("fakesink", "audiosink")
        self.playbin.set_property("audio-sink", self.audiosink)

        self.bus = self.playbin.get_bus()
        self.bus.add_signal_watch()
        self.watch_id = self.bus.connect("message", self.onBusMessage)

        self.playbin.set_property("uri", "file://%s" % filename)
        self.playbin.set_state(gst.STATE_PAUSED)

    def result (self):
        self.done.wait(5)
        self.disconnect()
        print self.success
        return self.success

    def onBusMessage(self, bus, message):
        confirmMainThread()
        if message.src == self.playbin:
            if message.type == gst.MESSAGE_STATE_CHANGED:
                prev, new, pending = message.parse_state_changed()
                if new == gst.STATE_PAUSED:
                    self.success = True
                    self.done.set()
                    # Success
            if message.type == gst.MESSAGE_ERROR:
                self.success = False
                self.done.set()

    @gtkAsyncMethod
    def disconnect (self):
        confirmMainThread()
        self.bus.disconnect (self.watch_id)
        self.playbin.set_state(gst.STATE_NULL)
        del self.bus
        del self.playbin
        del self.audiosink
        del self.videosink

class Extracter:
    def __init__(self, filename, thumbnail_filename, callback):
        confirmMainThread()
        self.thumbnail_filename = thumbnail_filename
        self.grabit = False
        self.first_pause = True
        self.success = False
        self.duration = 0
	self.buffer_probes = {}
        self.callback = callback

	self.pipeline = gst.parse_launch('filesrc location="%s" ! decodebin ! ffmpegcolorspace ! video/x-raw-rgb,depth=24,bpp=24 ! fakesink signal-handoffs=True' % (filename,))

        for sink in self.pipeline.sinks():
            name = sink.get_name()
            factoryname = sink.get_factory().get_name()
            if factoryname == "fakesink":
                pad = sink.get_pad("sink")
                self.buffer_probes[name] = pad.add_buffer_probe(self.buffer_probe_handler, name)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.watch_id = self.bus.connect("message", self.onBusMessage)

        self.pipeline.set_state(gst.STATE_PAUSED)

    def done (self):
        confirmMainThread()
        self.callback(self.success)

    def onBusMessage(self, bus, message):
        confirmMainThread()
        if message.src == self.pipeline:
            if message.type == gst.MESSAGE_STATE_CHANGED:
                prev, new, pending = message.parse_state_changed()
                if new == gst.STATE_PAUSED:
                    if self.first_pause:
                        self.duration = self.pipeline.query_duration(gst.FORMAT_TIME)[0]
                        self.grabit = True
                        seek_result = self.pipeline.seek(1.0,
                                gst.FORMAT_TIME,
                                gst.SEEK_FLAG_FLUSH | gst.SEEK_FLAG_ACCURATE,
                                gst.SEEK_TYPE_SET,
                                self.duration / 2,
                                gst.SEEK_TYPE_NONE, 0)
                        if not seek_result:
                            self.success = False
                            self.disconnect()
                    self.first_pause = False
            if message.type == gst.MESSAGE_ERROR:
                self.success = False
                self.disconnect()

    def buffer_probe_handler(self, pad, buffer, name) :
        """Capture buffers as gdk_pixbufs when told to."""
        if self.grabit:
            caps = buffer.caps
            if caps is not None:
                filters = caps[0]
                self.width = filters["width"]
                self.height = filters["height"]
            timecode = self.pipeline.query_position(gst.FORMAT_TIME)[0]
            pixbuf = gtk.gdk.pixbuf_new_from_data(buffer.data, gtk.gdk.COLORSPACE_RGB, False, 8, self.width, self.height, self.width * 3)
            pixbuf.save(self.thumbnail_filename, "png")
            del pixbuf
            self.success = True
            self.disconnect()
        return True

    @gtkAsyncMethod
    def disconnect (self):
        confirmMainThread()
        if self.pipeline is not None:
            self.pipeline.set_state(gst.STATE_NULL)
            for sink in self.pipeline.sinks():
                name = sink.get_name()
                factoryname = sink.get_factory().get_name()
                if factoryname == "fakesink" :
                    pad = sink.get_pad("sink")
                    pad.remove_buffer_probe(self.buffer_probes[name])
                    del self.buffer_probes[name]
            self.pipeline = None
        if self.bus is not None:
            self.bus.disconnect (self.watch_id)
            self.bus = None
        self.done()

class Renderer(app.VideoRenderer):
    def __init__(self):
        confirmMainThread()
        self.playbin = gst.element_factory_make("playbin", "player")
        self.bus = self.playbin.get_bus()
        self.bus.add_signal_watch()
        self.bus.enable_sync_message_emission()        
        self.watch_id = self.bus.connect("message", self.onBusMessage)
        self.bus.connect('sync-message::element', self.onSyncMessage)
        self.sink = gst.element_factory_make("ximagesink", "sink")
        self.playbin.set_property("video-sink", self.sink)

    def onSyncMessage(self, bus, message):
        if message.structure is None:
            return
        message_name = message.structure.get_name()
        if message_name == 'prepare-xwindow-id':
            imagesink = message.src
            imagesink.set_property('force-aspect-ratio', True)
            imagesink.set_xwindow_id(self.widget.window.xid)        
        
    def onBusMessage(self, bus, message):
        confirmMainThread()
        "recieves message posted on the GstBus"
        if message.type == gst.MESSAGE_ERROR:
            err, debug = message.parse_error()
            print "onBusMessage: gstreamer error: %s" % err
        elif message.type == gst.MESSAGE_EOS:
#            print "onBusMessage: end of stream"
            eventloop.addIdle(app.controller.playbackController.onMovieFinished,
                              "onBusMessage: skipping to next track")
        return None
            
    def setWidget(self, widget):
        confirmMainThread()
        widget.connect_after("realize", self.onRealize)
        widget.connect("unrealize", self.onUnrealize)
        widget.connect("expose-event", self.onExpose)
        self.widget = widget

    def onRealize(self, widget):
        confirmMainThread()
        self.gc = widget.window.new_gc()
        self.gc.foreground = gtk.gdk.color_parse("black")
        
    def onUnrealize(self, widget):
        confirmMainThread()
#        print "onUnrealize"
        self.sink = None
        
    def onExpose(self, widget, event):
        confirmMainThread()
        if self.sink:
            self.sink.expose()
        else:
            widget.window.draw_rectangle(self.gc,
                                         True,
                                         0, 0,
                                         widget.allocation.width,
                                         widget.allocation.height)
        return True

    def canPlayFile(self, filename):
        """whether or not this renderer can play this data"""
        return Tester(filename).result()

    def fillMovieData(self, filename, movie_data, callback):
        confirmMainThread()
        dir = os.path.join (config.get(prefs.ICON_CACHE_DIRECTORY), "extracted")
        try:
            os.makedirs(dir)
        except:
            pass
        screenshot = os.path.join (dir, os.path.basename(filename) + ".png")
        movie_data["screenshot"] = nextFreeFilename(screenshot)

        def handle_result (success):
            if success:
                movie_data["duration"] = extracter.duration / 1000000
                if not os.path.exists(movie_data["screenshot"]):
                    movie_data["screenshot"] = ""
            else:
                movie_data["screenshot"] = None
            callback(success)

	extracter = Extracter(filename, movie_data["screenshot"], handle_result)

    def goFullscreen(self):
        confirmMainThread()
        """Handle when the video window goes fullscreen."""
        print "haven't implemented goFullscreen method yet!"
        
    def exitFullscreen(self):
        confirmMainThread()
        """Handle when the video window exits fullscreen mode."""
        print "haven't implemented exitFullscreen method yet!"

    @gtkAsyncMethod
    def selectFile(self, filename):
        confirmMainThread()
        """starts playing the specified file"""
        self.stop()
        self.playbin.set_property("uri", "file://%s" % filename)
#        print "selectFile: playing file %s" % filename

    def getProgress(self):
        confirmMainThread()
        print "getProgress: what does this do?"

    @gtkSyncMethod
    def getCurrentTime(self):
        confirmMainThread()
        try:
            position, format = self.playbin.query_position(gst.FORMAT_TIME)
            position = position / 1000000000
        except Exception, e:
            print "getCurrentTime: caught exception: %s" % e
            position = 0
        return position

    def seek(self, seconds):
        confirmMainThread()
        event = gst.event_new_seek(1.0,
                                   gst.FORMAT_TIME,
                                   gst.SEEK_FLAG_FLUSH|gst.SEEK_FLAG_ACCURATE,
                                   gst.SEEK_TYPE_SET,
                                   seconds * 1000000000,
                                   gst.SEEK_TYPE_NONE, 0)
        result = self.playbin.send_event(event)
        if not result:
            print "seek failed"
        
    def playFromTime(self, seconds):
        confirmMainThread()
        #self.playbin.set_state(gst.STATE_NULL)
	self.seek(seconds)
        self.play()
#        print "playFromTime: starting playback from %s sec" % seconds

    def getDuration(self):
        confirmMainThread()
        try:
            duration, format = self.playbin.query_duration(gst.FORMAT_TIME)
            duration = duration / 1000000000
        except Exception, e:
            print "getDuration: caught exception: %s" % e
            duration = 1
        return duration

    @gtkAsyncMethod
    def reset(self):
        confirmMainThread()
        self.playbin.set_state(gst.STATE_NULL)
#        print "** RESET **"

    @gtkAsyncMethod
    def setVolume(self, level):
        confirmMainThread()
#        print "setVolume: set volume to %s" % level
        self.playbin.set_property("volume", level * 4.0)

    @gtkAsyncMethod
    def play(self):
        confirmMainThread()
        self.playbin.set_state(gst.STATE_PLAYING)
#        print "** PLAY **"

    @gtkAsyncMethod
    def pause(self):
        confirmMainThread()
        self.playbin.set_state(gst.STATE_PAUSED)
#        print "** PAUSE **"

    @gtkAsyncMethod
    def stop(self):
        confirmMainThread()
        self.playbin.set_state(gst.STATE_NULL)
#        print "** STOP **"
        
    def getRate(self):
        confirmMainThread()
        return 256
    
    def setRate(self, rate):
        confirmMainThread()
        print "setRate: set rate to %s" % rate
