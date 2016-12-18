import argparse
import sys
import os
import json
import math
import re
import datetime
import html
import subprocess

import requests
import appdirs
from ratelimit import *

__version__ = "0.1"

headers = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_7_3)'\
        'AppleWebKit/535.11 (KHTML, like Gecko) Chrome/17.0.963.79'\
        'Safari/535.11',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9'\
        ',*/*;q=0.8',
    'Accept-Encoding': 'gzip,deflate,sdch',
    'Accept-Language': 'en-US,en;q=0.8',
    'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.3'}


class GazelleSort:
    class RequestException(BaseException):
        pass

    def __init__(self, configdir):
        self.configfilepath = os.path.join(configdir, "config.json")
        self.error = False

        self.session = requests.Session()
        self.session.headers.update(headers)

        self.authkey = None
        self.snatches = []
        self.torrentdata = {}

        # Check if config file exists. If not, create default file.
        if not os.path.isfile(self.configfilepath):
            print("ERROR:")
            print("Config file not found at config path: %s" % self.configfilepath)
            print("A default config has been put in the location. Please edit it to your liking before running again.")

            with open(self.configfilepath, "w") as f:
                defaultconfig = {
                    "url": "",
                    "username": "",
                    "password": "",
                    "torrentdir": "",
                    "destdirs": {
                        "flac24bit": "",
                        "flac": "",
                        "320": "",
                        "v0": "",
                        "v2": ""
                    },
                    "pattern": {
                        "string": "%(artist)s - %(album)s (%(year)s) [%(format)s]",
                        "variousartists": "Various Artists",
                        "artistjoiner": " & ",
                        "listindividualartists": 2
                    }
                }

                f.write(json.dumps(defaultconfig, indent=4, sort_keys=True))

            self.error = True
            return
        else:
            with open(self.configfilepath, "r") as f:
                self.config = json.loads(f.read())

    def __del__(self):
        pass

    def checkConfig(self):
        """Checks if all necessary config options are present"""
        # Check if all required parameters exist
        necessaryparams = [("url", str), ("username", str), ("password", str), ("torrentdir", str), ("destdirs", dict), ("pattern", dict)]
        if not all(item in self.config for item in [x[0] for x in necessaryparams]):
            print("ERROR:")
            print("Required settings missing from config file:")
            for missing in [item for item in necessaryparams if item not in self.config]:
                print("  " + missing)
            return False

        # Check if all required parameters are of correct type
        if any([not isinstance(self.config[x[0]], x[1]) for x in necessaryparams]):
            print("ERROR:")
            print("Entries from config have wrong type:")
            for wrong in [x for x in necessaryparams if not isinstance(self.config[x[0]], x[1])]:
                print("  Param: '%s' (%s) should be of type: %s" % (wrong[0], type(self.config[wrong[0]]), wrong[1]))
            return False

        return True

    @rate_limited(0.5)
    def login(self):
        """Login and store cookie"""
        login = self.config["url"] + "/login.php"
        payload = {'username': self.config["username"],
                   'password': self.config["password"],
                   'keeplogged': 1,
                   'login': 'Login'
        }
        response = self.session.post(login, data=payload)
        return response.status_code

    @rate_limited(0.5)
    def logout(self):
        logout = self.config["url"] + "/logout.php"
        self.session.get(logout + "?auth=%s" % self.authkey)

    @rate_limited(0.5)
    def ajaxrequest(self, action, **kwargs):
        """API Request"""
        ajax = self.config["url"] + "/ajax.php"
        params = {"action": action}
        params.update(kwargs)

        if self.authkey is not None:
            params["auth"] = self.authkey

        response = self.session.get(ajax, params=params, allow_redirects=False)
        try:
            decoded = response.json()   # Get JSON from response
            if decoded["status"] == "success":
                return decoded  # If the api returns a success, return the entire api response
            else:   # Throw an exception if the api request itself is an error.
                if "error" in decoded:  # Check if there is an error message. Also redact the authkey for security reasons.
                    raise GazelleSort.RequestException("Request '%s' failed. Error: %s" % (re.sub("auth=[\da-fA-F]+", "auth=REDACTED", response.url), decoded["error"]))
                else:
                    raise GazelleSort.RequestException("Request '%s' failed. No Error message was returned by API." % response.url)
        except json.JSONDecodeError:
            raise GazelleSort.RequestException("Request didn't return any JSON. HTTP status code: %s" % response.status_code)

    @rate_limited(0.5)
    def getSnatched(self):
        """Find all torrents user has snatched"""
        torrents = self.config["url"] + "/torrents.php"

        profile = self.ajaxrequest("user", id=self.userid)
        pages = math.ceil(profile["response"]["community"]["seeding"] / 50)
        print("Reading IDs of %s snatched torrents" % profile["response"]["community"]["seeding"])

        @rate_limited(1)
        def readPage(page):
            r = self.session.get(torrents + "?type=seeding&page=%s&userid=%s" % (page + 1, self.userid))
            matches = re.findall("torrents.php\?id=(?P<groupid>\d+)&amp;torrentid=(?P<torrentid>\d+)", bytes.decode(r.content))
            self.snatches += list(map(lambda x: (int(x[0]), int(x[1])), matches))     # convert list of tuples from string to int

        for page in range(pages):
            readPage(page)

    def prepareDirs(self):
        """Check if destination folders exist, and create them if not"""
        for format in self.config["destdirs"]:
            if not os.path.isdir(self.config["destdirs"][format]):
                print("Creating dir: %s" % self.config["destdirs"][format])
                os.makedirs(self.config["destdirs"][format])

        if not os.path.isdir(self.config["torrentdir"]):
            raise Exception("Torrentdir (%s) doesn't exist or can't be accessed." % self.config["torrentdir"])

    def printProgressBar(self, progress, length, heartbeat=False):
        """Print a progress bar"""
        if heartbeat:
            print("\r⚡[", end="")
        else:
            print("\r [", end="")
        width = 1/length
        for i in range(length):
            fill = (progress - i*width) / width
            if fill < 0:
                print(" ", end="")
            elif fill < 0.25:
                print("▏", end="")
            elif fill < 0.5:
                print("▎", end="")
            elif fill < 0.75:
                print("▌", end="")
            elif fill < 1.0:
                print("▊", end="")
            else:
                print("█", end="")

        print("]", end="", flush=True)

    def scanTorrents(self):
        """Iterate through all torrents to fetch their data"""
        print("Fetching data of snatched torrents via API.")
        print("Due to rate limiting this will approximately take %s" % datetime.timedelta(seconds=len(self.snatches) * 2))

        for id in self.snatches:
            self.printProgressBar(self.snatches.index(id)/len(self.snatches), 40, bool(self.snatches.index(id)%2))
            self.torrentdata[id[1]] = self.ajaxrequest("torrent", id=id[1])["response"]

        print("\nDone!")

    def renderName(self, torrentdata):
        """Render name from supplied user settings"""
        if len(torrentdata["group"]["musicInfo"]["artists"]) > self.config["pattern"]["listindividualartists"]:
            artist = self.config["pattern"]["variousartists"]
        else:
            artist = self.config["pattern"]["artistjoiner"].join(sorted([artist["name"] for artist in torrentdata["group"]["musicInfo"]["artists"]]))

        fileformat = torrentdata["torrent"]["format"]

        formatdata = {
            "artist": artist,
            "album": torrentdata["group"]["name"],
            "year": torrentdata["group"]["year"],
            "format": fileformat
        }
        name = self.config["pattern"]["string"] % formatdata

        return name

    def processFiles(self):
        for key in self.torrentdata:
            torrentdata = self.torrentdata[key]

            # Check format
            format = torrentdata["torrent"]["format"]
            desttype = None
            if format == "":    # Non-music download, skip
                continue
            elif format == "FLAC":
                if torrentdata["torrent"]["encoding"] == "24bit Lossless":
                    desttype = "flac24bit"
                else:
                    desttype = "flac"
            elif format == "MP3":
                pass    # TODO: Detect mp3 types

            if desttype in self.config["destdirs"]:
                destdir = self.config["destdirs"][desttype]

                olddir = os.path.join(self.config["torrentdir"], torrentdata["torrent"]["filePath"].replace("/", "\\/"))
                newdir = os.path.join(destdir, self.renderName(torrentdata))

                # Create directory in destination and create hardlink to files
                subprocess.call(["cp", "-Rl", html.unescape(olddir), html.unescape(newdir)], stderr=subprocess.DEVNULL)

    def run(self):
        """Run the sorter"""
        # If there were any errors on initializing, quit.
        if self.error:
            return 1

        # Check the loaded config
        if not self.checkConfig():
            return 1

        # Login
        logincode = self.login()
        if not logincode == 200:
            print("ERROR:")
            print("Login request failed with code %s" % logincode)

        # Check if login was success
        indexjson = self.ajaxrequest("index")
        if indexjson and all([x in indexjson["response"] for x in ["authkey", "passkey", "id"]]):
            self.authkey = indexjson["response"]["authkey"]
            self.passkey = indexjson["response"]["passkey"]
            self.userid = indexjson["response"]["id"]

        # Fetch the group and torrent ids of all snatched torrents. This is redone on every program rerun,
        # and not cached, as it is likely to change a lot. If you know of any way to get this information directly from
        # the api, please drop me a message.
        self.getSnatched()

        # Prepare destination folders
        self.prepareDirs()

        # Scan torrents and check if they exist in torrentdir
        self.scanTorrents()

        # Proceed to process the files
        self.processFiles()

        self.logout()

        return 0


if __name__ == "__main__":
    appname = "GazelleSort"

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", type=str,
                        help="Override config directory, defaults to '%s'" % appdirs.user_config_dir(appname))

    args = parser.parse_args()

    # Store directory path
    configdir = appdirs.user_config_dir(appname)
    if args.config:
        configdir = args.config


    # If config directory does't exist, create it
    if not os.path.isdir(configdir):
        os.makedirs(configdir)

    # Start Gazellesort
    gs = GazelleSort(configdir)

    sys.exit(gs.run())

