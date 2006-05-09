from download_utils import grabURL
from HTMLParser import HTMLParser,HTMLParseError
import xml
from urlparse import urlparse, urljoin
from urllib import urlopen
from database import defaultDatabase
from item import *
from scheduler import ScheduleEvent
from copy import copy
from xhtmltools import unescape,xhtmlify,fixXMLHeader, fixHTMLHeader, toUTF8Bytes, urlencode
from cStringIO import StringIO
from threading import Thread, Semaphore
import traceback #FIXME get rid of this
from datetime import datetime, timedelta
from inspect import isfunction
from new import instancemethod
from iconcache import iconCacheUpdater, IconCache
import resource
import config
import os
import config
import re
import app
import views

whitespacePattern = re.compile(r"^[ \t\r\n]*$")

def defaultFeedIconURL():
    return resource.url("images/feedicon.png")

# Notes on character set encoding of feeds:
#
# The parsing libraries built into Python mostly use byte strings
# instead of unicode strings.  However, sometimes they get "smart" and
# try to convert the byte stream to a unicode stream automatically.
#
# What does what when isn't clearly documented
#
# We use the function toUTF8Bytes() to fix those smart conversions
#
# If you run into Unicode crashes, adding that function in the
# appropriate place should fix it.

# Universal Feed Parser http://feedparser.org/
# Licensed under Python license
import feedparser

# Pass in a connection to the frontend
def setDelegate(newDelegate):
    global delegate
    delegate = newDelegate

# Pass in a feed sorting function 
def setSortFunc(newFunc):
    global sortFunc
    sortFunc = newFunc

#
# Adds a new feed using USM
def addFeedFromFile(file):
    d = feedparser.parse(file)
    if d.feed.has_key('links'):
        for link in d.feed['links']:
            if link['rel'] == 'start':
                generateFeed(link['href'])
                return
    if d.feed.has_key('link'):
        addFeedFromWebPage(d.feed.link)

#
# Adds a new feed based on a link tag in a web page
def addFeedFromWebPage(url):
    feedURL = getFeedURLFromWebPage(url)
    if not feedURL is None:
        generateFeed(feedURL)

def getFeedURLFromWebPage(url):
    data = ''
    info = grabURL(url,"GET")
    if info is None:
        return None
    try:
        data = info['file-handle'].read()
        info['file-handle'].close()
    except:
        pass
    return HTMLFeedURLParser().getLink(info['updated-url'],data)

# URL validitation and normalization
def validateFeedURL(url):
    return re.match(r"^(http|https|feed)://[^/].*", url) is not None

def normalizeFeedURL(url):
    # Valid URL are returned as-is
    if validateFeedURL(url):
        return url
    
    # Check valid schemes with invalid separator
    match = re.match(r"^(http|https|feed):/*(.*)$", url)
    if match is not None:
        return "%s://%s" % match.group(1,2)

    # Replace invalid schemes by http
    match = re.match(r"^(.*:/*)*(.*)$", url)
    if match is not None:
        return "http://%s" % match.group(2)

    # We weren't able to normalize
    print "DTV: unable to normalize URL %s" % url
    return url

##
# Generates an appropriate feed for a URL
#
# @param url The URL of the feed
def generateFeed(url,ufeed):
    thread = Thread(target=lambda: _generateFeed(url,ufeed), \
                    name="generateFeed -- %s" % url)
    thread.setDaemon(False)
    thread.start()

def _generateFeed(url, ufeed, visible=True):
    if (url == "dtv:directoryfeed"):
        return DirectoryFeedImpl(ufeed)
    elif (url == "dtv:search"):
        return SearchFeedImpl(ufeed)
    elif (url == "dtv:searchDownloads"):
        return SearchDownloadsFeedImpl(ufeed)
    elif (url == "dtv:manualFeed"):
        return ManualFeedImpl(ufeed)

    info = grabURL(url,"GET")
    if info is None:
        return None
    try:
        modified = info['last-modified']
    except KeyError:
        modified = None
    try:
        etag = info['etag']
    except KeyError:
        etag = None
    #Definitely an HTML feed
    if (info['content-type'].startswith('text/html') or 
        info['content-type'].startswith('application/xhtml+xml')):
        #print "Scraping HTML"
        html = info['file-handle'].read()
        if info.has_key('charset'):
            html = fixHTMLHeader(html,info['charset'])
            charset = info['charset']
        else:
            charset = None
        info['file-handle'].close()
        if delegate.isScrapeAllowed(url):
            return ScraperFeedImpl(info['updated-url'],initialHTML=html,etag=etag,modified=modified, charset=charset, visible=visible, ufeed=ufeed)
        else:
            return None

    #It's some sort of feed we don't know how to scrape
    elif (info['content-type'].startswith('application/rdf+xml') or
          info['content-type'].startswith('application/atom+xml')):
        #print "ATOM or RDF"
        html = info['file-handle'].read()
        info['file-handle'].close()
        if info.has_key('charset'):
            xmldata = fixXMLHeader(html,info['charset'])
        else:
            xmldata = html
        return RSSFeedImpl(info['updated-url'],initialHTML=xmldata,etag=etag,modified=modified, visible=visible, ufeed=ufeed)
    # If it's not HTML, we can't be sure what it is.
    #
    # If we get generic XML, it's probably RSS, but it still could be
    # XHTML.
    #
    # application/rss+xml links are definitely feeds. However, they
    # might be pre-enclosure RSS, so we still have to download them
    # and parse them before we can deal with them correctly.
    elif (info['content-type'].startswith('application/rss+xml') or
          info['content-type'].startswith('application/podcast+xml') or
          info['content-type'].startswith('text/xml') or 
          info['content-type'].startswith('application/xml') or
          (info['content-type'].startswith('text/plain') and url.endswith('.xml'))):
        #print " It's doesn't look like HTML..."
        html = info["file-handle"].read()
        info["file-handle"].close()
        if info.has_key('charset'):
            xmldata = fixXMLHeader(html,info['charset'])
            html = fixHTMLHeader(html,info['charset'])
            charset = info['charset']
        else:
            xmldata = html
            charset = None
        try:
            parser = xml.sax.make_parser()
            parser.setFeature(xml.sax.handler.feature_namespaces, 1)
            handler = RSSLinkGrabber(info['redirected-url'],charset)
            parser.setContentHandler(handler)
            parser.parse(StringIO(xmldata))
        except xml.sax.SAXException: #it doesn't parse as RSS, so it must be HTML
            #print " Nevermind! it's HTML"
            if delegate.isScrapeAllowed(url):
                 return ScraperFeedImpl(info['updated-url'],initialHTML=html,etag=etag,modified=modified, charset=charset, visible=visible, ufeed=ufeed)
            else:
                 return None
        except UnicodeDecodeError:
            print "Unicode issue parsing... %s" % xmldata[0:300]
            traceback.print_exc()
            return None
        if handler.enclosureCount > 0 or handler.itemCount == 0:
            #print " It's RSS with enclosures"
            return RSSFeedImpl(info['updated-url'],initialHTML=xmldata,etag=etag,modified=modified, visible=visible, ufeed=ufeed)
        else:
            #print " It's pre-enclosure RSS"
            if delegate.isScrapeAllowed(url):
                return ScraperFeedImpl(info['updated-url'],initialHTML=xmldata,etag=etag,modified=modified, charset=charset, visible=visible, ufeed=ufeed)
            else:
                return None
    else:
        print "DTV doesn't know how to deal with "+info['content-type']+" feeds"
        return None

##
# Handle configuration changes so we can update feed update frequencies

def configDidChange(key, value):
    if key is config.CHECK_CHANNELS_EVERY_X_MN.key:
        for feed in views.feeds:
            updateFreq = 0
            try:
                updateFreq = feed.parsed["feed"]["ttl"]
            except:
                pass
            feed.setUpdateFrequency(updateFreq)

config.addChangeCallback(configDidChange)

##
# Actual implementation of a basic feed.
class FeedImpl:
    def __init__(self, url, ufeed, title = None, visible = True):
        self.available = 0
        self.unwatched = 0
        self.url = url
        self.ufeed = ufeed
        self.items = []
        if title == None:
            self.title = url
        else:
            self.title = title
        self.created = datetime.now()
        self.autoDownloadable = ufeed.initiallyAutoDownloadable
        self.startfrom = datetime.max
        self.getEverything = False
        self.maxNew = -1
        self.fallBehind = -1
        self.expire = "system"
        self.visible = visible
        self.updating = False
        self.lastViewed = datetime.min
        self.thumbURL = defaultFeedIconURL()
        self.initialUpdate = True
        self.updateFreq = config.get(config.CHECK_CHANNELS_EVERY_X_MN)*60
        self.expireTime = None

    # Sets the update frequency (in minutes). 
    # - A frequency of -1 means that auto-update is disabled.
    def setUpdateFrequency(self, frequency):
        try:
            frequency = int(frequency)
        except ValueError:
            frequency = -1

        if frequency < 0:
            self.cancelUpdateEvents()
            self.updateFreq = -1
        else:
            newFreq = max(config.get(config.CHECK_CHANNELS_EVERY_X_MN),
                          frequency)*60
            if newFreq != self.updateFreq:
                self.updateFreq = newFreq
                self.scheduleUpdateEvents(-1)

    def scheduleUpdateEvents(self, firstTriggerDelay):
        self.cancelUpdateEvents()
        if self.updateFreq > 0:
            self.scheduler = ScheduleEvent(self.updateFreq, self.update)
            if firstTriggerDelay >= 0:
                ScheduleEvent(firstTriggerDelay, self.update, False)

    def cancelUpdateEvents(self):
        try:
            self.scheduler.remove()
            self.scheduler = None
        except:
            pass

    # Subclasses should implement this
    def update(self):
        pass

    # Returns true iff this feed has been looked at
    def getViewed(self):
        ret = self.lastViewed != datetime.min
        return ret

    # Returns the ID of the actual feed, never that of the UniversalFeed wrapper
    def getFeedID(self):
        return self.getID()

    def getID(self):
        try:
            return self.ufeed.getID()
        except:
            print "%s has no ufeed" % self

    # Returns true if x is a newly available item, otherwise returns false
    def isAvailable(self, x):
        return x.creationTime > self.lastViewed and (x.getState() == 'stopped' or x.getState() == 'downloading')

    # Returns true if x is an unwatched item, otherwise returns false
    def isUnwatched(self, x):
        state = x.getState()
        return state == 'finished' or state == 'uploading'

    # Updates the state of unwatched and available items to meet
    # Returns true iff endChange() is called
    def updateUandA(self):
        # Note: I'm not locking this with the assumption that we don't
        #       care if these totals reflect an actual snapshot of the
        #       database. If items change in the middle of this, oh well.
        newU = 0
        newA = 0
        ret = False

        for item in self.items:
            if self.isAvailable(item):
                newA += 1
            if self.isUnwatched(item):
                newU += 1
        self.ufeed.beginRead()
        try:
            if newU != self.unwatched or newA != self.available:
                self.ufeed.beginChange()
                try:
                    ret = True
                    self.unwatched = newU
                    self.available = newA
                finally:
                    self.ufeed.endChange()
        finally:
            self.ufeed.endRead()
        return ret
            
    # Returns string with number of unwatched videos in feed
    def numUnwatched(self):
        return self.unwatched

    # Returns string with number of available videos in feed
    def numAvailable(self):
        return self.available

    # Returns true iff both unwatched and available numbers should be shown
    def showBothUAndA(self):
        return ((not self.isAutoDownloadable()) and
                self.unwatched > 0 and 
                self.available > 0)

    # Returns true iff unwatched should be shown and available shouldn't
    def showOnlyU(self):
        return ((self.unwatched > 0 and 
                 self.available == 0) or 
                (self.isAutoDownloadable() and
                 self.unwatched > 0))

    # Returns true iff available should be shown and unwatched shouldn't
    def showOnlyA(self):
        return ((not self.isAutoDownloadable()) and 
                self.unwatched == 0 and 
                self.available > 0)

    # Returns true iff neither unwatched nor available should be shown
    def showNeitherUNorA(self):
        return (self.unwatched == 0 and
                (self.isAutoDownloadable() or 
                 self.available == 0))

    ##
    # Sets the last time the feed was viewed to now
    def markAsViewed(self):
        # FIXME uncomment to make "new" state last 6 hours. See #655, #733
        self.lastViewed = datetime.now() #- timedelta(hours=6)
        self.updateUandA()

    ##
    # Returns true iff the feed is loading. Only makes sense in the
    # context of UniversalFeeds
    def isLoading(self):
        return False

    ##
    # Returns true iff this feed has a library
    def hasLibrary(self):
        return False

    ##
    # Downloads the next available item taking into account maxNew,
    # fallbehind, and getEverything
    def downloadNextAuto(self, dontUse = []):
        nextAuto = self.getNextAutoDownload()
        if nextAuto is not None:
            nextAuto.download(autodl=True)
            return True
        else:
            return False

    ##
    # Figure out the next available auto download item taking into account
    # maxNew, fallbehind, and getEverything
    def getNextAutoDownload(self, dontUse = []):
        self.ufeed.beginRead()
        try:
            next = None

            #The number of items downloading from this feed
            dling = 0
            #The number of items eligibile to download
            eligibile = 0
            #The number of unwatched, downloaded items
            newitems = 0

            #Find the next item we should get
            self.items.sort(sortFunc)
            for item in self.items:
                if (item.getState() == "autopending") and not item in dontUse:
                    eligibile += 1
                    if next == None:
                        next = item
                    elif item.getPubDateParsed() > next.getPubDateParsed():
                        next = item
                if item.getState() == "downloading":
                    dling += 1
                if item.getState() == "finished" or item.getState() == "uploading" and not item.getSeen():
                    newitems += 1

        finally:
            self.ufeed.endRead()

        if self.maxNew >= 0 and newitems >= self.maxNew:
            return None
        elif self.fallBehind>=0 and eligibile > self.fallBehind:
            dontUse.append(next)
            return self.getNextAutoDownload(dontUse)
        elif next != None:
            self.ufeed.beginRead()
            try:
                self.startfrom = next.getPubDateParsed()
            finally:
                self.ufeed.endRead()
            return next
        else:
            return None

    def downloadNextManual(self):
        self.ufeed.beginRead()
        next = None
        self.items.sort(sortFunc)
        for item in self.items:
            if item.getState() == "manualpending":
                if next is None:
                    next = item
                elif item.getPubDateParsed() < next.getPubDateParsed():
                    next = item
        if not next is None:
            next.download(autodl = False)
        self.ufeed.endRead()

    ##
    # Returns marks expired items as expired
    def expireItems(self):
        expireTime = datetime.max - datetime.min
        if self.expire == "feed":
            expireTime = self.expireTime
        elif self.expire == "system":
            expireTime = timedelta(days=config.get(config.EXPIRE_AFTER_X_DAYS))
            if expireTime <= timedelta(0):
                return
        elif self.expire == "never":
            return
        for item in self.items:
            local = item.getFilename() is not ""
            expiring = datetime.now() - item.getDownloadedTime() > expireTime
            stateOk = item.getState() in ('finished', 'stopped', 'watched')
            keepIt = item.getKeep()
            if local and expiring and stateOk and not keepIt:
                item.expire()

    ##
    # Returns true iff feed should be visible
    def isVisible(self):
        self.ufeed.beginRead()
        try:
            ret = self.visible
        finally:
            self.ufeed.endRead()
        return ret

    ##
    # Switch the auto-downloadable state
    def setAutoDownloadable(self, automatic):
        self.ufeed.beginRead()
        try:
            self.autoDownloadable = (automatic == "1")
            if self.autoDownloadable:
                self.startfrom = datetime.now()
            else:
                self.startfrom = datetime.max
        finally:
            self.ufeed.endRead()

    ##
    # Sets the 'getEverything' attribute, True or False
    def setGetEverything(self, everything):
        self.ufeed.beginRead()
        try:
            self.getEverything = everything
        finally:
            self.ufeed.endRead()

    ##
    # Sets the expiration attributes. Valid types are 'system', 'feed' and 'never'
    # Expiration time is in hour(s).
    def setExpiration(self, type, time):
        self.ufeed.beginRead()
        try:
            self.expire = type
            self.expireTime = timedelta(hours=time)

            if self.expire == "never":
                for item in self.items:
                    if item.getState() in ['finished','uploading','watched']:
                        item.setKeep(True)
        finally:
            self.ufeed.endRead()

    ##
    # Sets the maxNew attributes. -1 means unlimited.
    def setMaxNew(self, maxNew):
        self.ufeed.beginRead()
        try:
            self.maxNew = maxNew
        finally:
            self.ufeed.endRead()

    ##
    # Return the 'system' expiration delay, in days (can be < 1.0)
    def getDefaultExpiration(self):
        return float(config.get(config.EXPIRE_AFTER_X_DAYS))

    ##
    # Returns the 'system' expiration delay as a formatted string
    def getFormattedDefaultExpiration(self):
        expiration = self.getDefaultExpiration()
        formattedExpiration = ''
        if expiration < 0:
            formattedExpiration = 'never'
        elif expiration < 1.0:
            formattedExpiration = '%d hours' % int(expiration * 24.0)
        elif expiration == 1:
            formattedExpiration = '%d day' % int(expiration)
        elif expiration > 1 and expiration < 30:
            formattedExpiration = '%d days' % int(expiration)
        elif expiration >= 30:
            formattedExpiration = '%d months' % int(expiration / 30)
        return formattedExpiration

    ##
    # Returns "feed," "system," or "never"
    def getExpirationType(self):
        self.ufeed.beginRead()
        ret = self.expire
        self.ufeed.endRead()
        return ret

    ##
    # Returns"unlimited" or the maximum number of items this feed can fall behind
    def getMaxFallBehind(self):
        self.ufeed.beginRead()
        if self.fallBehind < 0:
            ret = "unlimited"
        else:
            ret = self.fallBehind
        self.ufeed.endRead()
        return ret

    ##
    # Returns "unlimited" or the maximum number of items this feed wants
    def getMaxNew(self):
        self.ufeed.beginRead()
        if self.maxNew < 0:
            ret = "unlimited"
        else:
            ret = self.maxNew
        self.ufeed.endRead()
        return ret

    ##
    # Returns the total absolute expiration time in hours.
    # WARNING: 'system' and 'never' expiration types return 0
    def getExpirationTime(self):
        delta = None
        self.ufeed.beginRead()
        try:
            expireAfterSetting = config.get(config.EXPIRE_AFTER_X_DAYS)
            if (self.expireTime is None or self.expire == 'never' or 
                    (self.expire == 'system' and expireAfterSetting <= 0)):
                return 0
            else:
                return (self.expireTime.days * 24 + 
                        self.expireTime.seconds / 3600)
        finally:
            self.ufeed.endRead()

    ##
    # Returns the number of days until a video expires
    def getExpireDays(self):
        ret = 0
        self.ufeed.beginRead()
        try:
            try:
                ret = self.expireTime.days
            except:
                ret = timedelta(days=config.get(config.EXPIRE_AFTER_X_DAYS)).days
        finally:
            self.ufeed.endRead()
        return ret

    ##
    # Returns the number of hours until a video expires
    def getExpireHours(self):
        ret = 0
        self.ufeed.beginRead()
        try:
            try:
                ret = int(self.expireTime.seconds/3600)
            except:
                ret = int(timedelta(days=config.get(config.EXPIRE_AFTER_X_DAYS)).seconds/3600)
        finally:
            self.ufeed.endRead()
        return ret
        

    ##
    # Returns true iff item is autodownloadable
    def isAutoDownloadable(self):
        self.ufeed.beginRead()
        ret = self.autoDownloadable
        self.ufeed.endRead()
        return ret

    def autoDownloadStatus(self):
        status = self.isAutoDownloadable()
        if status:
            return "ON"
        else:
            return "OFF"

    ##
    # Returns the title of the feed
    def getTitle(self):
        try:
            title = self.title
            if whitespacePattern.match(title):
                title = self.url
            return title
        except:
            return ""

    ##
    # Returns the URL of the feed
    def getURL(self):
        try:
            return self.url
        except:
            return ""

    ##
    # Returns the description of the feed
    def getDescription(self):
        return "<span />"

    ##
    # Returns a link to a webpage associated with the feed
    def getLink(self):
        return ""

    ##
    # Returns the URL of the library associated with the feed
    def getLibraryLink(self):
        return ""

    ##
    # Returns the URL of a thumbnail associated with the feed
    def getThumbnailURL(self):
        return self.thumbURL

    ##
    # Returns URL of license assocaited with the feed
    def getLicense(self):
        return ""

    ##
    # Returns the number of new items with the feed
    def getNewItems(self):
        self.ufeed.beginRead()
        count = 0
        for item in self.items:
            try:
                if item.getState() == 'finished' and not item.getSeen():
                    count += 1
            except:
                pass
        self.ufeed.endRead()
        return count

##
# This class is a magic class that can become any type of feed it wants
#
# It works by passing on attributes to the actual feed.
class Feed(DDBObject):
    def __init__(self,url, useThread=True, initiallyAutoDownloadable=True):
        self.origURL = url
        self.errorState = False
        self.initiallyAutoDownloadable = initiallyAutoDownloadable
        if useThread:
            self.loading = True
            self.actualFeed = FeedImpl(url,self)
            
            self.iconCache = IconCache(self, is_vital = True)
            DDBObject.__init__(self)
            
            thread = Thread(target=lambda: self.generateFeed(True), \
                            name="Feed.__init__ generate -- %s" % url)
            thread.setDaemon(False)
            thread.start()
        else:
            self.generateFeed(True)
            self.loading = False
            self.iconCache = IconCache(self, is_vital = True)
            DDBObject.__init__(self)

    # Returns javascript to mark the feed as viewed
    # FIXME: Using setTimeout is a hack to get around JavaScript bugs
    #        Without the timeout, the view is never completely updated
    def getMarkViewedJS(self):
        return ("function markViewed() {eventURL('action:markFeedViewed?url=%s');} setTimeout(markViewed, 5000);" % 
                urlencode(self.getURL()))

    # Returns the ID of this feed. Deprecated.
    def getFeedID(self):
        return self.getID()

    def getID(self):
        return DDBObject.getID(self)

    def hasError(self):
        ret = False
        self.beginRead()
        try:
            ret = self.errorState
        finally:
            self.endRead()
        return ret

    def getError(self):
        return "Could not load feed"

    def update(self):
        self.beginRead()
        try:
            if self.loading:
                return
            elif self.errorState:
                self.loading = True
                self.errorState = False
                self.beginChange()
                self.endChange()
                thread = Thread(target=lambda: self.generateFeed(), \
                                name="Feed.update generate -- %s" % \
                                self.origURL)
                thread.setDaemon(False)
                thread.start()
                return
        finally:
            self.endRead()
        self.actualFeed.update()

    def generateFeed(self, removeOnError=False):
        temp =  _generateFeed(self.origURL,self,visible=True)
        self.beginRead()
        try:
            self.loading = False
            if temp is None:
                self.errorState = True
            else:
                self.actualFeed = temp
        finally:
            self.endRead()

        if removeOnError and self.errorState:
            self.remove()
        else:
            self.beginChange()
            self.endChange()

    def getActualFeed(self):
        return self.actualFeed

    def __getattr__(self,attr):
        return getattr(self.getActualFeed(),attr)

    def remove(self):
        self.beginChange()
        self.cancelUpdateEvents()
        try:
            DDBObject.remove(self)
            for item in self.items:
                if not item.getKeep():
                    item.expire()
                item.remove()
        finally:
            self.endChange()

    def getThumbnail(self):
        self.beginRead()
        try:
            if self.iconCache.isValid():
                basename = os.path.basename(self.iconCache.filename)
                return resource.iconCacheUrl(basename)
            else:
                return defaultFeedIconURL()
        finally:
            self.endRead()

    def updateIcons(self):
        iconCacheUpdater.clearVital()
        for item in self.items:
            item.iconCache.requestUpdate(True)
        for feed in views.feeds:
            feed.iconCache.requestUpdate(True)

    def onRestore(self):
        if (self.iconCache == None):
            self.iconCache = IconCache (self, is_vital = True)
        else:
            self.iconCache.dbItem = self
            self.iconCache.requestUpdate(True)

    def __str__(self):
        return "Feed - %s" % self.getTitle()

class RSSFeedImpl(FeedImpl):
    firstImageRE = re.compile('\<\s*img\s+[^>]*src\s*=\s*"(.*?)"[^>]*\>',re.I|re.M)
    
    def __init__(self,url,ufeed,title = None,initialHTML = None, etag = None, modified = None, visible=True):
        FeedImpl.__init__(self,url,ufeed,title,visible=visible)
        self.initialHTML = initialHTML
        self.etag = etag
        self.modified = modified
        self.scheduleUpdateEvents(0)

    ##
    # Returns the description of the feed
    def getDescription(self):
        self.ufeed.beginRead()
        try:
            ret = xhtmlify('<span>'+unescape(self.parsed.summary)+'</span>')
        except:
            ret = "<span />"
        self.ufeed.endRead()
        return ret

    ##
    # Returns a link to a webpage associated with the feed
    def getLink(self):
        self.ufeed.beginRead()
        try:
            ret = self.parsed.link
        except:
            ret = ""
        self.ufeed.endRead()
        return ret

    ##
    # Returns the URL of the library associated with the feed
    def getLibraryLink(self):
        self.ufeed.beginRead()
        try:
            ret = self.parsed.libraryLink
        except:
            ret = ""
        self.ufeed.endRead()
        return ret        

    def hasVideoFeed(self, enclosures):
        hasOne = False
        for enclosure in enclosures:
            if isVideoEnclosure(enclosure):
                hasOne = True
                break
        return hasOne

    ##
    # Updates a feed
    def update(self):
        info = {}
        self.ufeed.beginRead()
        try:
            if self.updating:
                return
            else:
                self.updating = True
        finally:
            self.ufeed.endRead()
        if hasattr(self, 'initialHTML') and self.initialHTML is not None:
            html = self.initialHTML
            self.initialHTML = None
        else:
            try:
                etag = self.etag
            except:
                etag = None
            try:
                modified = self.modified
            except:
                modified = None
            info = grabURL(self.url,etag=etag,modified=modified)
            if info is None:
                self.ufeed.beginRead()
                try:
                    self.updating = False
                finally:
                    self.finishUpdate()
                return None
            
            html = info['file-handle'].read()
            info['file-handle'].close()
            if info.has_key('charset'):
                html = fixXMLHeader(html,info['charset'])
            if info['status'] == 304:
                self.ufeed.beginRead()
                try:
                    self.updating = False
                finally:
                    self.finishUpdate()
                return
            self.url = info['updated-url']
        d = feedparser.parse(html)
        self.parsed = d

        self.ufeed.beginRead()
        try:
            try:
                self.title = self.parsed["feed"]["title"]
            except KeyError:
                try:
                    self.title = self.parsed["channel"]["title"]
                except KeyError:
                    pass
            if (self.parsed.feed.has_key('image') and 
                self.parsed.feed.image.has_key('url')):
                self.thumbURL = self.parsed.feed.image.url
                self.ufeed.iconCache.requestUpdate(is_vital=True)
            for entry in self.parsed.entries:
                entry = self.addScrapedThumbnail(entry)
                new = True
                for item in self.items:
                    try:
                        if item.getRSSID() == entry["id"]:
                            item.update(entry)
                            new = False
                    except KeyError:
                        # If the item changes at all, it results in a
                        # new entry
                        if (item.getRSSEntry() == entry):
                            item.update(entry)
                            new = False
                if (new and entry.has_key('enclosures') and
                    self.hasVideoFeed(entry.enclosures)):
                    self.items.append(Item(self.ufeed,entry))
            try:
                updateFreq = self.parsed["feed"]["ttl"]
            except KeyError:
                updateFreq = 0
            self.setUpdateFrequency(updateFreq)
            
            if self.initialUpdate:
                self.initialUpdate = False
                sortedItems = list(self.items)
                sortedItems.sort(lambda x, y: cmp(x.getPubDateParsed(), y.getPubDateParsed()))
                self.startfrom = sortedItems[-1].getPubDateParsed()
            
            self.updating = False
        finally:
            self.finishUpdate(info)

    def finishUpdate(self, info=None):
        if info is not None:
            if info.has_key('etag'):
                self.etag = info['etag']
            if info.has_key('last-modified'):
                self.modified = info['last-modified']
        self.ufeed.endRead() #FIXMENOW This is sloow...
        if not self.updateUandA():
            self.ufeed.beginChange()
            self.ufeed.endChange()

    def addScrapedThumbnail(self,entry):
        if (entry.has_key('enclosures') and len(entry['enclosures'])>0 and
            entry.has_key('description') and 
            not entry['enclosures'][0].has_key('thumbnail')):
                desc = RSSFeedImpl.firstImageRE.search(unescape(entry['description']))
                if not desc is None:
                    entry['enclosures'][0]['thumbnail'] = FeedParserDict({'url': desc.expand("\\1")})
        return entry

    ##
    # Returns the URL of the license associated with the feed
    def getLicense(self):
        try:
            ret = self.parsed.license
        except:
            ret = ""
        return ret

    ##
    # Called by pickle during deserialization
    def onRestore(self):
        #self.itemlist = defaultDatabase.filter(lambda x:isinstance(x,Item) and x.feed is self)
        #FIXME: the update dies if all of the items aren't restored, so we 
        # wait a little while before we start the update
        self.updating = False
        self.scheduleUpdateEvents(0.1)


##
# A DTV Collection of items -- similar to a playlist
class Collection(FeedImpl):
    def __init__(self,ufeed,title = None):
        FeedImpl.__init__(self,ufeed,url = "dtv:collection",title = title,visible = False)

    ##
    # Adds an item to the collection
    def addItem(self,item):
        if isinstance(item,Item):
            self.ufeed.beginRead()
            try:
                self.removeItem(item)
                self.items.append(item)
            finally:
                self.ufeed.endRead()
            return True
        else:
            return False

    ##
    # Moves an item to another spot in the collection
    def moveItem(self,item,pos):
        self.ufeed.beginRead()
        try:
            self.removeItem(item)
            if pos < len(self.items):
                self.items[pos:pos] = [item]
            else:
                self.items.append(item)
        finally:
            self.ufeed.endRead()

    ##
    # Removes an item from the collection
    def removeItem(self,item):
        self.ufeed.beginRead()
        try:
            for x in range(0,len(self.items)):
                if self.items[x] == item:
                    self.items[x:x+1] = []
                    break
        finally:
            self.ufeed.endRead()
        return True

##
# A feed based on un unformatted HTML or pre-enclosure RSS
class ScraperFeedImpl(FeedImpl):
    #FIXME: change this to a higher number once we optimize a bit
    maxThreads = 1

    def __init__(self,url,ufeed, title = None, visible = True, initialHTML = None,etag=None,modified = None,charset = None):
        FeedImpl.__init__(self,url,ufeed,title,visible)
        self.initialHTML = initialHTML
        self.initialCharset = charset
        self.linkHistory = {}
        self.linkHistory[url] = {}
        self.tempHistory = {}
        if not etag is None:
            self.linkHistory[url]['etag'] = etag
        if not modified is None:
            self.linkHistory[url]['modified'] = modified
        self.semaphore = Semaphore(ScraperFeedImpl.maxThreads)
        self.scheduleUpdateEvents(0)
        self.setUpdateFrequency(360)

    def getMimeType(self,link):
        info = grabURL(link,"HEAD")
        if info is None:
            return ''
        else:
            return info['content-type']

    ##
    # This puts all of the caching information in tempHistory into the
    # linkHistory. This should be called at the end of an updated so that
    # the next time we update we don't unnecessarily follow old links
    def saveCacheHistory(self):
        self.ufeed.beginRead()
        try:
            for url in self.tempHistory.keys():
                self.linkHistory[url] = self.tempHistory[url]
            self.tempHistory = {}
        finally:
            self.ufeed.endRead()
    ##
    # returns a tuple containing the text of the URL, the url (in case
    # of a permanent redirect), a redirected URL (in case of
    # temporary redirect)m and the download status
    def getHTML(self, url, useActualHistory = True):
        etag = None
        modified = None
        if self.linkHistory.has_key(url):
            if self.linkHistory[url].has_key('etag'):
                etag = self.linkHistory[url]['etag']
            if self.linkHistory[url].has_key('modified'):
                modified = self.linkHistory[url]['modified']
        info = grabURL(url, etag=etag, modified=modified)
        if info is None:
            return (None, url, url,404, None)
        else:
            if not self.tempHistory.has_key(info['updated-url']):
                self.tempHistory[info['updated-url']] = {}
            if info.has_key('etag'):
                self.tempHistory[info['updated-url']]['etag'] = info['etag']
            if info.has_key('last-modified'):
                self.tempHistory[info['updated-url']]['modified'] = info['last-modified']

            html = info['file-handle'].read()
            #print "Scraper got HTML of length "+str(len(html))
            info['file-handle'].close()
            #print "Closed"
            if info.has_key('charset'):
                return (html, info['updated-url'],info['redirected-url'],info['status'],info['charset'])
            else:
                return (html, info['updated-url'],info['redirected-url'],info['status'],None)

    def addVideoItem(self,link,dict,linkNumber):
        link = link.strip()
        if dict.has_key('title'):
            title = dict['title']
        else:
            title = link
        for item in self.items:
            if item.getURL() == link:
                return
        if dict.has_key('thumbnail') > 0:
            i=Item(self.ufeed, FeedParserDict({'title':title,'enclosures':[FeedParserDict({'url':link,'thumbnail':FeedParserDict({'url':dict['thumbnail']})})]}),linkNumber = linkNumber)
        else:
            i=Item(self.ufeed, FeedParserDict({'title':title,'enclosures':[FeedParserDict({'url':link})]}),linkNumber = linkNumber)
        self.items.append(i)
        if not self.updateUandA():
            self.ufeed.beginChange()
            self.ufeed.endChange()

    def makeProcessLinkFunc(self,subLinks,depth,linkNumber):
        return lambda: self.processLinksThenFreeSem(subLinks,depth,linkNumber)

    def processLinksThenFreeSem(self,subLinks,depth,linkNumber):
        try:
            self.processLinks(subLinks, depth,linkNumber)
        finally:
            #print "Releasing semaphore"
            self.semaphore.release()

    #FIXME: compound names for titles at each depth??
    def processLinks(self,links, depth = 0,linkNumber = 0):
        maxDepth = 2
        urls = links[0]
        links = links[1]
        if depth<maxDepth:
            for link in urls:
                if depth == 0:
                    linkNumber += 1
                #print "Processing %s (%d)" % (link,linkNumber)

                # FIXME: Using file extensions totally breaks the
                # standard and won't work with Broadcast Machine or
                # Blog Torrent. However, it's also a hell of a lot
                # faster than checking the mime type for every single
                # file, so for now, we're being bad boys. Uncomment
                # the elif to make this use mime types for HTTP GET URLs

                if ((link[-4:].lower() in 
                    ['.mov','.wmv','.mp4','.m4v','.mp3','.ogg','.anx','.mpg','.avi']) or
                    (link[-5:].lower() in ['.mpeg'])):
                    mimetype = 'video/unknown'
                elif link[-8:].lower() == '.torrent':
                    mimetype = "application/x-bittorrent"
                #elif link.find('?') > 0 and link.lower().find('.htm') == -1:
                #    mimetype = self.getMimeType(link)
                #    #print " mimetype is "+mimetype
                else:
                    mimetype = 'text/html'
                if mimetype != None:
                    #This is text of some sort: HTML, XML, etc.
                    if ((mimetype.startswith('text/html') or
                         mimetype.startswith('application/xhtml+xml') or 
                         mimetype.startswith('text/xml')  or
                         mimetype.startswith('application/xml') or
                         mimetype.startswith('application/rss+xml') or
                         mimetype.startswith('application/podcast+xml') or
                         mimetype.startswith('application/atom+xml') or
                         mimetype.startswith('application/rdf+xml') ) and
                        depth < maxDepth -1):
                        (html, url, redirURL,status,charset) = self.getHTML(link)
                        if status == 304: #It's cached
                            pass
                        elif not html is None:
                            subLinks = self.scrapeLinks(html, redirURL,charset=charset)
                            if depth == 0:
                                self.semaphore.acquire()
                                #print "Acquiring semaphore"
                                thread = Thread(target = self.makeProcessLinkFunc(subLinks,depth+1,linkNumber), \
                                                name = "scraper processLinks -- %s" % self.url)
                                thread.setDaemon(False)
                                thread.start()
                            else:
                                self.processLinks(subLinks,depth+1,linkNumber)
                        else:
                            pass
                            #print link+" seems to be bogus..."
                    #This is a video
                    elif (mimetype.startswith('video/') or 
                          mimetype.startswith('audeo/') or
                          mimetype == "application/ogg" or
                          mimetype == "application/x-annodex" or
                          mimetype == "application/x-bittorrent"):
                        self.addVideoItem(link, links[link],linkNumber)

    #FIXME: go through and add error handling
    def update(self):
        self.ufeed.beginRead()
        try:
            if self.updating:
                return
            else:
                self.updating = True
        finally:
            self.ufeed.endRead()
        if not self.initialHTML is None:
            html = self.initialHTML
            self.initialHTML = None
            redirURL=self.url
            status = 200
            charset = self.initialCharset
            self.initialCharset = None
        else:
            (html,url, redirURL, status,charset) = self.getHTML(self.url)
        if not status == 304:
            if not html is None:
                links = self.scrapeLinks(html, redirURL, setTitle=True,charset=charset)
                self.processLinks(links)
            #Download the HTML associated with each page
        self.ufeed.beginRead()
        try:
            self.saveCacheHistory()
            self.updating = False
        finally:
            self.ufeed.endRead()

    def scrapeLinks(self,html,baseurl,setTitle = False,charset = None):
        try:
            if not charset is None:
                xmldata = fixXMLHeader(html,charset)
                html = fixHTMLHeader(html,charset)
            else:
                xmldata = html
            parser = xml.sax.make_parser()
            parser.setFeature(xml.sax.handler.feature_namespaces, 1)
            if not charset is None:
                handler = RSSLinkGrabber(baseurl,charset)
            else:
                handler = RSSLinkGrabber(baseurl)
            parser.setContentHandler(handler)
            try:
                parser.parse(StringIO(xmldata))
            except IOError, e:
                pass
            links = handler.links
            linkDict = {}
            for link in links:
                if link[0].startswith('http://') or link[0].startswith('https://'):
                    if not linkDict.has_key(toUTF8Bytes(link[0],charset)):
                        linkDict[toUTF8Bytes(link[0])] = {}
                    if not link[1] is None:
                        linkDict[toUTF8Bytes(link[0])]['title'] = toUTF8Bytes(link[1],charset).strip()
                    if not link[2] is None:
                        linkDict[toUTF8Bytes(link[0])]['thumbnail'] = toUTF8Bytes(link[2],charset)
            if setTitle and not handler.title is None:
                self.ufeed.beginChange()
                try:
                    self.title = toUTF8Bytes(handler.title)
                finally:
                    self.ufeed.endChange()
            return ([x[0] for x in links if x[0].startswith('http://') or x[0].startswith('https://')], linkDict)
        except (xml.sax.SAXException, IOError):
            (links, linkDict) = self.scrapeHTMLLinks(html,baseurl,setTitle=setTitle, charset=charset)
            return (links, linkDict)

    ##
    # Given a string containing an HTML file, return a dictionary of
    # links to titles and thumbnails
    def scrapeHTMLLinks(self,html, baseurl,setTitle=False, charset = None):
        #print "Scraping "+baseurl+" as HTML"
        lg = HTMLLinkGrabber()
        links = lg.getLinks(html, baseurl)
        if setTitle and not lg.title is None:
            self.ufeed.beginChange()
            try:
                self.title = toUTF8Bytes(lg.title)
            finally:
                self.ufeed.endChange()
            
        linkDict = {}
        for link in links:
            if link[0].startswith('http://') or link[0].startswith('https://'):
                if not linkDict.has_key(toUTF8Bytes(link[0],charset)):
                    linkDict[toUTF8Bytes(link[0])] = {}
                if not link[1] is None:
                    linkDict[toUTF8Bytes(link[0])]['title'] = toUTF8Bytes(link[1],charset).strip()
                if not link[2] is None:
                    linkDict[toUTF8Bytes(link[0])]['thumbnail'] = toUTF8Bytes(link[2],charset)
        return ([x[0] for x in links if x[0].startswith('http://') or x[0].startswith('https://')],linkDict)
        
    ##
    # Called by pickle during deserialization
    def onRestore(self):
        #self.itemlist = defaultDatabase.filter(lambda x:isinstance(x,Item) and x.feed is self)

        #FIXME: the update dies if all of the items aren't restored, so we 
        # wait a little while before we start the update
        self.updating = False
        self.tempHistory = {}
        self.scheduleUpdateEvents(.1)
        self.semaphore = Semaphore(ScraperFeedImpl.maxThreads)

##
# A feed of all of the Movies we find in the movie folder that don't
# belong to a "real" feed.  If the user changes her movies folder, this feed
# will continue to remember movies in the old folder.
#
class DirectoryFeedImpl(FeedImpl):

    def __init__(self,ufeed):
        FeedImpl.__init__(self,url = "dtv:directoryfeed",ufeed=ufeed,title = "Feedless Videos",visible = False)

        self.setUpdateFrequency(5)
        self.scheduleUpdateEvents(0)

    ##
    # Directory Items shouldn't automatically expire
    def expireItems(self):
        pass

    def setUpdateFrequency(self, frequency):
        newFreq = frequency*60
        if newFreq != self.updateFreq:
                self.updateFreq = newFreq
                self.scheduleUpdateEvents(-1)

    def update(self):
        self.ufeed.beginRead()
        try:
            if self.updating:
                return
            else:
                self.updating = True
        finally:
            self.ufeed.endRead()
        #Files known about by real feeds
        knownFiles = set()
        for item in views.items:
            if not item.feed is self.ufeed:
                for f in item.getFilenames():
                    knownFiles.add(os.path.normcase(f))
        #Remove items that are in feeds, but we have in our list
        # NOTE: we rely on the fact that all our items are single files, so we
        # only need to use getFilename(), instead of getFilenames().
        self.ufeed.beginChange()
        try:
            for x in reversed(range(len(self.items))):
                if self.items[x].getFilename() in knownFiles:
                    self.items[x].remove()
                    del self.items[x]
        finally:
            self.ufeed.endChange()

        self.ufeed.beginRead()
        try:
            myFiles = set(x.getFilename() for x in self.items)
        finally:
            self.ufeed.endRead()

        #Adds any files we don't know about
        #Files on the filesystem
        moviesDir = config.get(config.MOVIES_DIRECTORY)
        if os.path.isdir(moviesDir):
            existingFiles = [os.path.normcase(os.path.join(moviesDir, f)) 
                    for f in os.listdir(moviesDir)]
            toAdd = []
            for file in existingFiles:
                if (os.path.isfile(file) and os.path.basename(file)[0] != '.' and 
                        not file in knownFiles and not file in myFiles):
                    toAdd.append(file)
            self.ufeed.beginChange()
            try:
                for file in toAdd:
                    self.items.append(FileItem(self.ufeed, file))
            finally:
                self.ufeed.endChange()
        self.updating = False

    def onRestore(self):
        #FIXME: the update dies if all of the items aren't restored, so we 
        # wait a little while before we start the update
        self.updating = False
        self.scheduleUpdateEvents(.1)


##
# Search and Search Results feeds

class SearchFeedImpl (RSSFeedImpl):
    
    def __init__(self, ufeed):
        RSSFeedImpl.__init__(self, url='', ufeed=ufeed, title='dtv:search', visible=False)
        self.setUpdateFrequency(-1)
        self.setAutoDownloadable(False)
        self.searching = False
        self.lastEngine = 'yahoo'
        self.lastQuery = ''

    def getStatus(self):
        status = 'idle-empty'
        if self.searching:
            status =  'searching'
        elif len(self.items) > 0:
            status =  'idle-with-results'
        return status

    def reset(self, url='', searchState=False):
        self.ufeed.beginChange()
        try:
            for item in self.items:
                item.remove()
            self.items = []
            self.url = url
            self.searching = searchState
        finally:
            self.ufeed.endChange()
    
    def preserveDownloads(self, downloadsFeed):
        self.ufeed.beginRead()
        try:
            allItems = [] + self.items
            for item in allItems:
                if item.getState() != 'stopped':
                    downloadsFeed.addItem(item)
        finally:
            self.ufeed.endRead()
        
    def lookup(self, engine, query):
        url = self.getRequestURL(engine, query)
        self.reset(url, True)
        self.lastQuery = query
        thread = Thread(target=self.update, \
                        name = "%s search -- %s" % (engine, query))
        thread.setDaemon(False)
        thread.start()

    def getRequestURL(self, engine, query, filterAdultContents=True, limit=50):
        if query == "LET'S TEST DTV'S CRASH REPORTER TODAY":
            someVariable = intentionallyUndefinedVariableToTestCrashReporter

        if engine == 'yahoo':
            url =  "http://api.search.yahoo.com/VideoSearchService/rss/videoSearch.xml"
            url += "?appid=dtv_search"
            url += "&adult_ok=%d" % int(not filterAdultContents)
            url += "&results=%d" % limit
            url += "&format=any"
            url += "&query=%s" % urlencode(query)
        elif engine == 'blogdigger':
            url =  "http://blogdigger.com/media/rss.jsp"
            url += "?q=%s" % urlencode(query)
            url += "&media=video"
            url += "&media=torrent"
            url += "&sortby=date"
        return url

    def update(self):
        if self.url is not None and self.url != '':
            RSSFeedImpl.update(self)

    def finishUpdate(self, info=None):
        self.searching = False
        RSSFeedImpl.finishUpdate(self, info)

class SearchDownloadsFeedImpl(FeedImpl):
    def __init__(self, ufeed):
        FeedImpl.__init__(self, url='dtv:searchDownloads', ufeed=ufeed, 
                title=None, visible=False)
        self.setUpdateFrequency(-1)

    def addItem(self, item):
        self.ufeed.beginRead()
        try:
            if not item in self.items:
                item.beginRead()
                try:
                    item.feed.items.remove(item)
                    item.feed = self.ufeed
                finally:
                    item.endRead()
                self.items.append(item)
        finally:
            self.ufeed.endRead()

class ManualFeedImpl(FeedImpl):
    """Videos/Torrents that have been added using by the user opening them
    with democracy.
    """

    def __init__(self, ufeed):
        FeedImpl.__init__(self, url='dtv:manualFeed', ufeed=ufeed, 
                title=None, visible=False)
        self.expire = 'never'
        self.setUpdateFrequency(-1)
        
    def addItem(self, item):
        self.ufeed.beginRead()
        try:
            if not item in self.items:
                self.items.append(item)
        finally:
            self.ufeed.endRead()

##
# Parse HTML document and grab all of the links and their title
# FIXME: Grab link title from ALT tags in images
# FIXME: Grab document title from TITLE tags
class HTMLLinkGrabber(HTMLParser):
    linkPattern = re.compile("^.*?<(a|embed)\s.*?(href|src)\s*=\s*\"(.*?)\".*?>(.*?)</a>(.*)$", re.S)
    imgPattern = re.compile(".*<img\s.*?src\s*=\s*\"(.*?)\".*?>", re.S)
    tagPattern = re.compile("<.*?>")
    def getLinks(self,data, baseurl):
        self.links = []
        self.lastLink = None
        self.inLink = False
        self.inObject = False
        self.baseurl = baseurl
        self.inTitle = False
        self.title = None
        self.thumbnailUrl = None

        match = HTMLLinkGrabber.linkPattern.match(data)
        while match:
            link = urljoin(baseurl, match.group(3))
            desc = match.group(4)
            imgMatch = HTMLLinkGrabber.imgPattern.match(desc)
            if imgMatch:
                thumb = urljoin(baseurl, imgMatch.group(1))
            else:
                thumb = None
            desc =  HTMLLinkGrabber.tagPattern.sub(' ',desc)
            self.links.append( (link, desc, thumb))
            match = HTMLLinkGrabber.linkPattern.match(match.group(5))
        return self.links

class RSSLinkGrabber(xml.sax.handler.ContentHandler):
    def __init__(self,baseurl,charset=None):
        self.baseurl = baseurl
        self.charset = charset
    def startDocument(self):
        #print "Got start document"
        self.enclosureCount = 0
        self.itemCount = 0
        self.links = []
        self.inLink = False
        self.inDescription = False
        self.inTitle = False
        self.inItem = False
        self.descHTML = ''
        self.theLink = ''
        self.title = None
        self.firstTag = True

    def startElementNS(self, name, qname, attrs):
        (uri, tag) = name
        if self.firstTag:
            self.firstTag = False
            if tag != 'rss':
                raise xml.sax.SAXNotRecognizedException, "Not an RSS file"
        if tag.lower() == 'enclosure' or tag.lower() == 'content':
            self.enclosureCount += 1
        elif tag.lower() == 'link':
            self.inLink = True
            self.theLink = ''
        elif tag.lower() == 'description':
            self.inDescription = True
            self.descHTML = ''
        elif tag.lower() == 'item':
            self.itemCount += 1
            self.inItem = True
        elif tag.lower() == 'title' and not self.inItem:
            self.inTitle = True
    def endElementNS(self, name, qname):
        (uri, tag) = name
        if tag.lower() == 'description':
            lg = HTMLLinkGrabber()
            try:
                html = xhtmlify(unescape(self.descHTML),addTopTags=True)
                if not self.charset is None:
                    html = fixHTMLHeader(html,self.charset)
                self.links[:0] = lg.getLinks(html,self.baseurl)
            except HTMLParseError: # Don't bother with bad HTML
                print "DTV: bad HTML in %s" % self.baseurl
            self.inDescription = False
        elif tag.lower() == 'link':
            self.links.append((self.theLink,None,None))
            self.inLink = False
        elif tag.lower() == 'item':
            self.inItem == False
        elif tag.lower() == 'title' and not self.inItem:
            self.inTitle = False

    def characters(self, data):
        if self.inDescription:
            self.descHTML += data
        elif self.inLink:
            self.theLink += data
        elif self.inTitle:
            if self.title is None:
                self.title = data
            else:
                self.title += data

# Grabs the feed link from the given webpage
class HTMLFeedURLParser(HTMLParser):
    def getLink(self,baseurl,data):
        self.baseurl = baseurl
        self.link = None
        try:
            self.feed(data)
        except HTMLParseError:
            print "DTV: error parsing "+str(baseurl)
        try:
            self.close()
        except HTMLParseError:
            print "DTV: error closing "+str(baseurl)
        return self.link

    def handle_starttag(self, tag, attrs):
        attrdict = {}
        for (key, value) in attrs:
            attrdict[key.lower()] = value
        if (tag.lower() == 'link' and attrdict.has_key('rel') and 
            attrdict.has_key('type') and attrdict.has_key('href') and
            attrdict['rel'].lower() == 'alternate' and 
            attrdict['type'].lower() in ['application/rss+xml',
                                         'application/podcast+xml',
                                         'application/rdf+xml',
                                         'application/atom+xml',
                                         'text/xml',
                                         'application/xml']):
            self.link = urljoin(self.baseurl,attrdict['href'])
