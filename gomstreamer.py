#!/usr/bin/env python2
# -*- coding: utf-8 -*-

'''
Copyright 2010 Simon Potter, Tomáš Heřman
Copyright 2011 Simon Potter
Copyright 2011 Fj (fj.mail@gmail.com)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

 http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import cookielib
import datetime
import logging as log
import os
import re
import StringIO
import sys
import time
import urllib
import urllib2
from optparse import OptionParser
from os import path as os_path
from string import Template
from urlparse import urljoin

debug = False
debug = True  # Uncomment this line to print more debugging information

# send INFO and above messages to stdout, if debug enabled then also send 
# everything to 'gomstreamer.log' (because dumps of webpage contents are necessary but distracting)
log.basicConfig(level = log.INFO,
                stream = sys.stdout,
                format='%(levelname)s %(message)s')
if debug:
    logfile_handler = log.FileHandler('gomstreamer.log')
    logfile_handler.setFormatter(log.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.getLogger().addHandler(logfile_handler)

VERSION = '0.8.0'

def main():
    curlCmd = 'curl -A KPeerClient "$url" -o "$output"'
    wgetCmd = 'wget -U KPeerClient --tries 1 "$url" -O "$output"'
    vlcPath, webCmdDefault = getDefaultLocations(curlCmd, wgetCmd)
    vlcCmdDefault = vlcPath + ' --file-caching $cache $debug - vlc://quit'
    options, args = parseOptions(vlcCmdDefault, webCmdDefault)

    # Printing out parameters
    log.debug('Email: %r', options.email)
    log.debug('Password: %r', options.password)
    log.debug('Mode: %r', options.mode)
    log.debug('Quality: %r', options.quality)
    log.debug('Output: %r', options.outputFile)
    log.debug('VlcCmd: %r', options.vlcCmd)
    log.debug('WebCmd: %r', options.webCmd)
    log.debug('AlternativeStream: %r', options.alternativeStream)

    # Stopping if email and password are defaults found in *.sh/command/cmd
    if options.email == 'youremail@example.com' and options.password == 'PASSWORD':
        log.error('Enter your GOMtv email and password into your *.sh, *.command, or *.cmd file.')
        log.error('\nThis script will not work correctly without a valid account.')
        return False

    # Seeing if we're running the latest version of GOMstreamer
    checkForUpdate()

    if options.mode == 'scheduled-save':
        # Delaying execution until necessary
        if not delay(options.kt): return False

    # Setting urllib2 up so that we can store cookies
    cookiejar = cookielib.LWPCookieJar()
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cookiejar))
    urllib2.install_opener(opener)

    # Signing into GOMTV
    log.info('Signing in')
    signIn('https://ssl.gomtv.net/userinfo/loginProcess.gom', options)
    if len(cookiejar) == 0:
        log.error('Authentification failed. Please check your login and password.')
        return False


    log.info('Getting live page url')
    gomtvURL = 'http://www.gomtv.net'
    for liveURL in iteratePossibleLivePageURLs(gomtvURL, options.alternativeStream):
        try:
            liveURL = urljoin(gomtvURL, liveURL) # will work correctly both for relative and absolute addresses.
            log.info('Trying the Live page at: %r' % liveURL)
            contents, quality = grabLivePage(liveURL, options.quality)

            log.info('Parsing the Live page for the GOX XML link.')
            url = parseLivePage(contents, quality)

            # Grab the contents of the URL listed on the Live page for a stream
            log.info('Grabbing the GOX XML file.')
            goxFile = grabPage(url)

            # Find out the URL found in the response
            log.info('Parsing the GOX XML file for the stream URL.')
            url = parseGOXFile(goxFile)
            break
        except Exception as exc:
            log.exception('Failed to extract stream url from %r', liveURL)


    # Put variables into VLC command
    vlcCmd = Template(options.vlcCmd).substitute(
            {'cache': options.cache, 
             'debug' : ('', '--verbose=2')[debug]})

    # Put variables into wget/curl command
    outputFile = '-' if options.mode == 'play' else options.outputFile
    webCmd = Template(options.webCmd).substitute(
            {'url' : url, 'output' : outputFile})

    # Add verbose output for VLC if we are debugging
    if debug:
        webCmd = webCmd + ' -v'

    # If playing pipe wget/curl into VLC, else save stream to file
    # We have already substituted $output with correct target.
    if options.mode == 'play':
        cmd = webCmd + ' | ' + vlcCmd
    else:
        cmd = webCmd

    log.info('')
    log.info('Stream URL: %r', url)
    log.info('')
    log.info('Command: %r', cmd)
    log.info('')

    if options.mode == 'play':
        log.info('Playing stream...')
    else:
        log.info('Saving stream as %r ...', outputFile)

    # Executing command
    try:
        os.system(cmd)
    except KeyboardInterrupt:
        # Swallow it, we are terminating anyway and don't want a stack trace.
        pass
    return True

def signIn(gomtvSignInURL, options):
    values = {
             'cmd': 'login',
             'rememberme': '1',
             'mb_username': options.email,
             'mb_password': options.password
             }
    data = urllib.urlencode(values)
    # Now expects to log in only via the website. Thanks chrippa.
    headers = {'Referer': 'http://www.gomtv.net/'}
    request = urllib2.Request(gomtvSignInURL, data, headers)
    response = urllib2.urlopen(request)
    # The real response that we want are the cookies, so returning None is fine.
    return

def grabLivePage(gomtvLiveURL, quality):
    contents = grabPage(gomtvLiveURL)
    # If a special event occurs, we know that the live page contents
    # will just be some JavaScript that redirects the browser to the
    # real live page. We assume that the entirety of this JavaScript
    # is less than 200 characters long, and that real live pages are
    # more than that.
    if len(contents) < 200:
        log.info('Live page source too short, assuming Live Event redirect')
        match = re.search(' \"(.*)\";', contents)
        assert match, 'Redirect URL not found'
        gomtvLiveURL = urljoin(gomtvLiveURL, match.group(1))
        log.info('Redirecting to the Event\'s \'Live\' page (%r).' % gomtvLiveURL)
        contents = grabPage(gomtvLiveURL)
        # Most events are free and have both HQ and SQ streams, but
        # not SQTest. As a result, assume we really want SQ after asking
        # for SQTest, makes it more seamless between events and GSL.
        if quality == "SQTest":
            quality = "SQ"
    return contents, quality

def grabPage(url):
    response = urllib2.urlopen(url)
    contents = response.read()
    log.debug('Got this from %r:\n%s', url, contents)
    return contents

def parseOptions(vlcCmdDefault, webCmdDefault):
    # Collecting options parsed in from the command line
    parser = OptionParser()
    parser.add_option('-p', '--password', dest = 'password', help = 'Password to your GOMtv account')
    parser.add_option('-e', '--email', dest = 'email', help = 'Email your GOMtv account uses')
    parser.add_option('-m', '--mode', dest = 'mode',
                      help = 'Mode of use: "play", "save" or "scheduled-save". Default is "play". This parameter is case sensitive.',
                      choices=['play', 'save', 'scheduled-save'])
    parser.add_option('-q', '--quality', dest = 'quality', help = 'Stream quality to use: "HQ", "SQ" or "SQTest". Default is "SQTest". This parameter is case sensitive.')
    parser.add_option('-o', '--output', dest = 'outputFile', help = 'File to save stream to (Default = "dump.ogm")')
    parser.add_option('-t', '--time', dest = 'kt', help = 'If the "scheduled-save" mode is used, this option holds the value of the *Korean* time to record at in HH:MM format. (Default = "18:00")')
    parser.add_option('-v', '--vlccmd', '-c', '--command', dest = 'vlcCmd', help = 'Custom command for playing stream from stdout')
    parser.add_option('-w', '--webcmd', dest = 'webCmd', help = 'Custom command for producing stream on stdout')
    parser.add_option('-d', '--buffer-time', dest = 'cache', type = 'int', help = 'VLC cache size in [ms]')
    parser.add_option('-a', '--alternative-stream', dest = 'alternativeStream', type = 'int', 
                      help = 'Use N-th stream link found on the Live page. Zero-based, will use the last link if N >= number of links.')

    parser.set_defaults(
            vlcCmd = vlcCmdDefault,
            webCmd = webCmdDefault,
            quality = 'SQTest',         # Setting default stream quality to 'SQTest'
            outputFile = 'dump.ogm',    # Save to dump.ogm by default
            mode = 'play',              # Want to play the stream by default
            kt = '18:00',               # If we are scheduling a recording, do it at 18:00 KST by default
            cache = 30000,              # Caching 30s by default
            alternativeStream = None    # Do not use the alt-stream search path at all.
            )
    options, args = parser.parse_args()
    # additional sanity checks
    if len(args):
        parser.error('Extra arguments specified: ' + repr(args))
    if not options.email:
        parser.error('--email must be specified')
    if not options.password:
        parser.error('--password must be specified')
    return options, args

def getDefaultLocations(curlCmd, wgetCmd):
    # Application locations and parameters for different operating systems.
    if os.name == 'posix' and os.uname()[0] == 'Darwin':
        # OSX
        vlcPath = '/Applications/VLC.app/Contents/MacOS/VLC'
        webCmdDefault = curlCmd
    elif os.name == 'posix':
        # Linux
        vlcPath = 'vlc'
        webCmdDefault = wgetCmd
    elif os.name == 'nt':
        def find_vlc():
            vlc_subpath = r'VideoLAN\VLC\vlc.exe'
            prog_files = os.environ.get('ProgramFiles')
            prog_files86 = os.environ.get('ProgramFiles(x86)')
            # 32bit Python on x64 Windows would see both as mapping to the x86
            # folder, but that's OK since there's no official 64bit vlc for
            # Windows yet.
            vlc_path = os_path.join(prog_files, vlc_subpath) if prog_files else None
            if vlc_path and os_path.exists(vlc_path):
                return vlc_path
            vlc_path = os_path.join(prog_files86, vlc_subpath) if prog_files86 else None
            if vlc_path and os_path.exists(vlc_path):
                return vlc_path
            return 'vlc' # maybe it's in PATH

        vlcPath = '"' + find_vlc() + '"'
        webCmdDefault = curlCmd
    else:
        assert False, 'Unrecognized OS'
    return vlcPath, webCmdDefault

def checkForUpdate():
    log.info('Checking for update...')
    try:
        # Grabbing txt file containing version string of latest version
        updateURL = 'http://sjp.co.nz/projects/gomstreamer/version.txt'
        latestVersion = grabPage(updateURL).strip()

        if VERSION < latestVersion:
            log.info('================================================================================')
            log.info('')
            log.info(' NOTE: Your version of GOMstreamer is ' + VERSION + '.')
            log.info('       The latest version is ' + latestVersion + '.')
            log.info('       Download the latest version from http://sjp.co.nz/projects/gomstreamer/')
            log.info('')
            log.info('================================================================================')
        else:
            log.info('have the latest version')
    except Exception as exc:
        log.error('Failed to check version: %s', exc)
        # ignore the error.
        # also don't use log.exception because we aren't particularly interested in a traceback here.

def delay(kt):
    KST = kt.split(':')
    korean_hours, korean_minutes = map(int, KST)

    # Checking to see whether we have valid times
    if korean_hours < 0 or korean_hours > 23 or \
       korean_minutes < 0 or korean_minutes > 59:
        log.error('Enter in a valid time in the format HH:MM. HH = hours [0-23], MM = minutes [0-59].')

    current_utc_time = datetime.datetime.utcnow()
    # Korea is 9 hours ahead of UTC
    current_korean_time = current_utc_time + datetime.timedelta(hours = 9)
    target_korean_time = datetime.datetime(current_korean_time.year,
                                           current_korean_time.month,
                                           current_korean_time.day,
                                           korean_hours,
                                           korean_minutes)

    # If the current korean time is after our target time, we assume that
    # delayed recording is for the following evening
    if current_korean_time > target_korean_time:
        target_korean_time = target_korean_time + datetime.timedelta(days = 1)

    # Finding out the length of time to sleep for
    # and enabling nice printing of the time.
    record_delta = (target_korean_time - current_korean_time).total_seconds()
    minutes, seconds = divmod(record_delta, 60)
    hours, minutes = divmod(minutes, 60)
    nice_record_delta = '%dh %dm %ds' % (hours, minutes, seconds)

    log.info('Waiting until %s KST.', kt)
    log.info('This will occur after waiting ' + nice_record_delta + '.')
    log.info('')
    try:
        time.sleep(record_delta)  # Delaying further execution until target Korean time
        return True
    except KeyboardInterrupt:
        log.info('Scheduling has been cancelled.')
        return False

def iteratePossibleLivePageURLs(gomtvURL, alternativeStream = None):
    def internal_iterator():
        if alternativeStream is not None:
            yield getLivePageURL_gom(gomtvURL, alternativeStream)
            yield '/main/goLive.gom'
        else:
            yield '/main/goLive.gom'
            yield getLivePageURL_gom(gomtvURL)
        yield getSeasonURL_sjp()
    return (url for url in internal_iterator() if url) # must use a generator instead of list comprehension!

def getLivePageURL_sjp():
    # Grab the txt file containing URL string of latest season
    try:
        sjp_season_url = 'http://sjp.co.nz/projects/gomstreamer/season1.txt'
        return grabPage(sjp_season_url).strip()
    except Exception as exc:
        log.error('Failed to get live page url from %r: %s', sjp_season_url, exc)

def getLivePageURL_gom(gomtvURL, alternativeStream = 0):
    # Getting season url from the 'Go Live!' button on the main page. 
    try: 
        contents = grabPage(gomtvURL)
        matches = re.findall('a href="([^"]*)" class="nowbtn" title="([^"]*)"', contents)
        assert matches, 'no live page urls found in page'
        # otherwise do not treat absense of an alternative stream as error.
        url, title = matches[min(alternativeStream, len(matches) - 1)]
        log.info('Found live page url for %r: %r' % (title, url))
        return url
    except Exception as exc:
        log.error('Failed to get live page url from the \'Go Live\' button: %s', exc)

def parseLivePage(contents, quality):
    # Parsing through the live page for a link to the gox XML file.
    # Quality is simply passed as a URL parameter e.g. HQ, SQ, SQTest
    try:
        patternHTML = r'[^/]+var.+(http://www.gomtv.net/gox[^;]+;)'
        urlFromHTML = re.search(patternHTML, contents).group(1)
        urlFromHTML = re.sub(r'\" \+ playType \+ \"', quality, urlFromHTML)
    except AttributeError:
        log.error('Unable to find the GOMtv XML URL on the Live page.')
        raise

    # Finding the title of the stream, probably not necessary but
    # done for completeness
    try:
        patternTitle = r'this\.title[^;]+;'
        titleFromHTML = re.search(patternTitle, contents).group(0)
        titleFromHTML = re.search(r'\"(.*)\"', titleFromHTML).group(0)
        titleFromHTML = re.sub(r'"', '', titleFromHTML)
        urlFromHTML = re.sub(r'"\+ tmpThis.title;', titleFromHTML, urlFromHTML)
    except AttributeError:
        log.error('Unable to find the stream title on the Live page.')
        raise

    return urlFromHTML

def parseGOXFile(contents):
    # The response for the GOX XML if an incorrect stream quality is chosen is 1002.
    if contents == '1002':
        log.error('A premium ticket is required to watch higher quality streams, please choose "SQTest" instead.')
        assert False

    # Grabbing the gomcmd URL
    try:
        log.info('Parsing for the HTTP stream.')
        streamPattern = r'<REF href="([^"]*)"/>'
        regexResult = re.search(streamPattern, contents).group(1)
    except AttributeError:
        log.error('Unable to find the gomcmd URL in the GOX XML file.')
        raise

    log.info('Stream found, cleaning up URL.')
    regexResult = urllib.unquote(regexResult)
    regexResult = re.sub(r'&amp;', '&', regexResult)
    # SQ and SQTest streams can be gomp2p links, with actual stream address passed as a parameter.
    if regexResult.startswith('gomp2p://'):
        log.info('Extracting stream URL from gomp2p link.')
        regexResult, n = re.subn(r'^.*LiveAddr=', '', regexResult)
        if not n:
            log.warning('Failed to extract stream URL from %r', regexResult)
    # Cosmetics, getting rid of the HTML entity, we don't
    # need either of the " character or &quot;
    regexResult = regexResult.replace('&quot;', '')
    return regexResult

# Actually run the script
if __name__ == '__main__':
    sys.exit(0 if main() else 1)
