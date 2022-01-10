#!/usr/bin/env python
from base64 import b64decode
import cgi
from collections import namedtuple
from datetime import datetime
import functools
import html
import json
import logging
import re
import time
import uuid

from bs4 import BeautifulSoup
import requests
import stravalib
from stravalib.model import Activity, Bike as _Bike
from stravaweblib.model import (DataFormat, ScrapedShoe, Bike, ScrapedBike,
                                ScrapedBikeComponent, ScrapedActivity,
                                ScrapedActivityPhoto, Athlete, ScrapedAthlete,
                                ScrapedChallenge, FrameType)


__log__ = logging.getLogger(__name__)


# Used for filtering when scraping the activity list
ACTIVITY_WORKOUT_TYPES = {
    "Ride": {None: 10, "Race": 11, "Workout": 12},
    "Run": {None: 0, "Race": 1, "Long Run": 2, "Workout": 3}
}

# Regexes for pulling information out of the activity details page
PHOTOS_REGEX = re.compile(r"var\s+photosJson\s*=\s*(\[.*\]);")
ATHLETE_REGEX = re.compile(r"var\s+currentAthlete\s*=\s*new\s+Strava.Models.CurrentAthlete\(({.*})\);")
CHALLENGE_IDS_REGEX = re.compile(r"var\s+trophiesAnalyticsProperties\s*=\s*{.*challenge_id:\s*\[(\[[\d\s,]*\])\]")
PAGE_VIEW_REGEX = re.compile(r"pageView\s*=\s*new\s+Strava.Labs.Activities.Pages.(\S+)PageView\([\"']?\d+[\"']?,\s*[\"']([^\"']+)")
CHALLENGE_REGEX = re.compile(r"var\s+challenge\s*=\s*new\s+Strava.Models.Challenge\(({.*})\);")
CHALLENGE_DATE_REGEX = re.compile(r"(\S{3} \d{2}, \d{4}) to (\S{3} \d{2}, \d{4})")

NON_NUMBERS = re.compile(r'[^\d\.]')

ExportFile = namedtuple("ExportFile", ("filename", "content"))
ActivityFile = ExportFile  # TODO: deprecate and remove


class ScrapingError(ValueError):
    """An error that is retured when something fails during scraping

    This can happen because something on the website changed.
    """


class ScrapingClient:
    """
    A client that uses web scraping to interface with Strava.

    Can be used as a mixin to add the extra methods to the main stravalib.Client
    """

    def __init__(self, *args, **kwargs):
        # Docstring set manually after class definition

        jwt = kwargs.pop("jwt", None)
        email = kwargs.pop("email", None)
        password = kwargs.pop("password", None)

        self._csrf = kwargs.pop("csrf", None)

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

        if jwt:
            self._login_with_jwt(jwt)
            __log__.info("Resumed session using JWT '%s'", jwt)
        elif email and password:
            self._login_with_password(email, password)
            __log__.info("Logged in as '%s'", email)
        else:
            raise ValueError("'jwt' or both of 'email' and 'password' are required")

        super().__init__(*args, **kwargs)

    @property
    def jwt(self):
        return self._session.cookies.get('strava_remember_token')

    @property
    def csrf(self):
        if not self._csrf:
            self._csrf = self._get_csrf_token()
        return self._csrf

    @property
    def athlete_id(self):
        return int(self._session.cookies.get('strava_remember_id'))

    def request(self, method, service, *args, **kwargs):
        """Request a URL from Strava

        :service: The URL to send the request to without the base URL
        """
        return self._session.request(method, "https://www.strava.com/{}".format(service), *args, **kwargs)

    def request_head(self, service, *args, **kwargs):
        return self.request("HEAD", service, *args, **kwargs)

    def request_get(self, service, *args, **kwargs):
        return self.request("GET", service, *args, **kwargs)

    def request_post(self, service, *args, **kwargs):
        return self.request("POST", service, *args, **kwargs)

    def _get_csrf_token(self):
        """Get a CSRF token

        Uses the about page because it's small and doesn't redirect based
        on if the client is logged in or not.
        """
        soup = BeautifulSoup(self.request_get("about").text, 'html5lib')

        try:
            head = soup.head
            csrf_param = head.find('meta', attrs={"name": "csrf-param"}).attrs['content']
            csrf_token = head.find('meta', attrs={"name": "csrf-token"}).attrs['content']
        except (AttributeError, KeyError):
            # "AttributeError: 'NoneType' object has no attr..." when failing
            # to find the tags.
            raise stravalib.exc.Fault("Couldn't find CSRF token")

        return {csrf_param: csrf_token}

    def _login_with_jwt(self, jwt):
        """Log in using the strava_remember_token (a JWT) from a previous session"""
        # The JWT's 'sub' key contains the id of the account. This must be
        # extracted and set as the 'strava_remember_id' cookie.
        try:
            payload = jwt.split('.')[1]  # header.payload.signature
            payload += "=" * (4 - len(payload) % 4)  # ensure correct padding
            data = json.loads(b64decode(payload))
        except Exception:
            raise ValueError("Failed to parse JWT payload")

        try:
            if data["exp"] < time.time():
                raise ValueError("JWT has expired")
            web_id = str(data["sub"])
        except KeyError:
            raise ValueError("Failed to extract required data from the JWT")

        self._session.cookies.set('strava_remember_id', web_id, domain='.strava.com', secure=True)
        self._session.cookies.set('strava_remember_token', jwt, domain='.strava.com', secure=True)

    def _login_with_password(self, email, password):
        """Log into the website using a username and password"""
        resp = self.request_post(
            "session",
            allow_redirects=False,
            data={
                "email": email,
                "password": password,
                "remember_me": "on",
                **self.csrf
            }
        )
        if not resp.is_redirect or resp.next.url.endswith("/login"):
            raise stravalib.exc.LoginFailed("Couldn't log in to website, check creds")

    def get_extra_activity_details(self, activity_id):
        """Scapes the full activity page for various details

        Returns a dict of the properties
        """
        __log__.debug("Getting extra information for activity %s", activity_id)
        resp = self.request_get("activities/{}".format(activity_id))
        if not resp.ok:
            raise stravalib.exc.Fault("Failed to load activity page to get details")

        ret = {}

        soup = BeautifulSoup(resp.text, 'html5lib')

        summary = soup.find("div", class_="activity-summary-container")
        if summary:
            name = summary.find("h1", class_="activity-name")
            if name:
                ret["name"] = name.text.strip()
            description = summary.find("div", class_="activity-description")
            if description:
                ret["description"] = description.text.strip()
            device = summary.find("div", class_="device")
            if device:
                ret["device_name"] = device.text.strip()

        for script in soup.find_all("script"):
            if "var pageView;" in script.text:
                m = PAGE_VIEW_REGEX.search(script.text)
                if not m:
                    __log__.error("Failed to extract manual and type data from page")
                    continue
                ret["manual"] = m.group(1).lower() == "manual"
                ret["type"] = m.group(2)

            elif "var photosJson" in script.text:
                m = PHOTOS_REGEX.search(script.text)
                if not m:
                    __log__.error("Failed to extract photo data from page")
                    continue
                try:
                    photos = json.loads(m.group(1))
                except (TypeError, ValueError) as e:
                    __log__.error("Failed to parse extracted photo data", exc_info=True)
                    continue
                ret["photos"] = [ScrapedActivityPhoto(**p) for p in photos]

        return ret

    def get_activity_photos(self, activity_id, size=None, only_instagram=None):
        """A scraping-based alternative to stravalib.Client.get_activity_photos

        :param activity_id: The activity for which to fetch photos.
        :param size: [unused] (for compatbility with stravalib)
        :param only_instagram: [unused] (for compatibility with stravalib)

        :return: A list of ScrapedActivityPhoto objects
        """
        return self.get_extra_activity_details(activity_id).get("photos", None)

    def get_activities(self, keywords=None, activity_type=None, workout_type=None,
                       commute=False, is_private=False, indoor=False, gear_id=None,
                       before=None, after=None, limit=None):
        """A scraping-based alternative to stravalib.Client.get_activities

        Note that when using multiple parameters they are treated as AND, not OR

        :param keywords: Text to search for
        :param activity_type: The type of the activity. See stravalib.model:Activity.TYPES
        :param workout_type: The type of workout ("Race", "Workout", etc)
        :param commute: Only return activities marked as commutes
        :param is_private: Only return private activities
        :param indoor: Only return indoor/trainer activities
        :param gear_id: Only return activities using this gear

        Parameters for compatibility with stravalib.Client.get_activities:

        :param before: Result will start with activities whose start date is
                       before specified date. (UTC)
        :param after: Result will start with activities whose start date is after
                      specified value. (UTC)
        :param limit: How many maximum activities to return.

        :yield: ScrapedActivity objects
        """

        __log__.debug("Getting activities")
        if activity_type is not None and activity_type not in Activity.TYPES:
            raise ValueError(
                "Invalid activity type. Must be one of: {}".format(",".join(Activity.TYPES))
            )

        if activity_type in ACTIVITY_WORKOUT_TYPES:
            workout_type = ACTIVITY_WORKOUT_TYPES[activity_type].get(workout_type)
            if workout_type is None:
                raise ValueError(
                    "Invalid workout type for a {}. Must be one of: {}".format(
                        activity_type,
                        ", ".join(ACTIVITY_WORKOUT_TYPES[activity_type].keys())
                    )
                )
        elif workout_type is not None or gear_id is not None:
            raise ValueError(
                "Can only filter using workout type of gear when activity type is one of: {}".format(
                    ", ".join(ACTIVITY_WORKOUT_TYPES.keys())
                )
            )

        before = stravalib.Client._utc_datetime_to_epoch(None, before or datetime.max)
        after = stravalib.Client._utc_datetime_to_epoch(None, after or datetime.min)

        num_yielded = 0
        page = 1
        per_page = 20
        search_session_id = uuid.uuid4()

        conv_bool = lambda x: "" if not x else "true"

        while True:
            __log__.debug("Getting page %s of activities", page)
            resp = self.request_get(
                "athlete/training_activities",
                headers= {
                    "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript",
                    "X-Requested-With": "XMLHttpRequest",
                },
                params={
                    "search_session_id": search_session_id,
                    "page": page,
                    "per_page": per_page,
                    "keywords": keywords,
                    "new_activity_only": "false",
                    "activity_type": activity_type or "",
                    "commute": conv_bool(commute),
                    "private_activities": conv_bool(is_private),
                    "trainer": conv_bool(indoor),
                    "gear": gear_id or "",
                    "order": "start_date_local DESC" # Return in reverse-chronological order
                }
            )
            if resp.status_code != 200:
                raise stravalib.exc.Fault(
                    "Failed to list activities (status code {})".format(resp.status_code)
                )
            try:
                data = resp.json()["models"]
            except (ValueError, TypeError, KeyError) as e:
                raise ScrapingError("Invalid JSON response from Strava") from e

            # No results = done
            if not data:
                return

            for activity in data:
                # Respect the limit
                if limit is not None and num_yielded >= limit:
                    return

                # Translate workout types from ints back to strings
                wt = activity.pop("workout_type")
                if activity["type"] in ACTIVITY_WORKOUT_TYPES:
                    for k, v in ACTIVITY_WORKOUT_TYPES[activity["type"]].items():
                        if wt == v:
                            activity["workout_type"] = k
                            break

                activity = ScrapedActivity(bind_client=self, **activity)

                # Respect the before and after filters
                # Will see activities from neweset to oldest so can do less
                # work to limit by time
                ts = activity.start_date.timestamp()
                if ts < after:
                    # Activity is too new, no more results
                    return
                elif ts > before:
                    # Activity is too old, don't yield it
                    continue

                yield activity
                num_yielded += 1

            page += 1

    def get_activity(self, activity_id):
        """A scraping-based alternative to stravalib.Client.get_activity

        Note that this actually performs a search for the activity using
        `get_activities` to get most of the information. Generally, it would be
        more efficient to use `get_activities` to find the activities directly.
        """
        d = self.get_extra_activity_details(activity_id)
        for x in self.get_activities(keywords=d["name"], activity_type=d["type"]):
            if x.id == activity_id:
                x._do_expand(d, overwrite=False)
                return x

    def delete_activity(self, activity_id):
        """
        Deletes the specified activity.

        :param activity_id: The activity to delete.
        :type activity_id: int
        """
        __log__.debug("Deleting activity %s", activity_id)
        resp = self.request_post(
            "activities/{}".format(activity_id),
            allow_redirects=False,
            data={
                "_method": "delete",
                **self.csrf
            }
        )

        if not resp.is_redirect or not resp.next.url.endswith("/athlete/training"):
            raise stravalib.exc.Fault(
                "Failed to delete activity (status code: {})".format(resp.status_code),
            )

    @staticmethod
    def _make_export_file(resp, id_):
        # Get file name from request (if possible)
        content_disposition = resp.headers.get("content-disposition", "")
        filename = cgi.parse_header(content_disposition)[1].get("filename")

        # Sane default for filename
        if not filename:
            filename = str(id_)

        # Note that Strava always removes periods from the filename so if one
        # exists we know it's for the extension
        if "." not in filename:
            if fmt is DataFormat.ORIGINAL:
                ext = 'dat'
            else:
                ext = fmt
            filename = "{}.{}".format(filename, ext)

        # Return the filename and an iterator to download the file with
        return ExportFile(
            filename=filename,
            content=resp.iter_content(chunk_size=16*1024)  # 16KB
        )

    def get_activity_data(self, activity_id, fmt=DataFormat.ORIGINAL, json_fmt=None):
        """
        Get a file containing the provided activity's data

        The returned data can either be the original file that was uploaded,
        a GPX file, or a TCX file.

        :param activity_id: The activity to retrieve.
        :type activity_id: int

        :param fmt: The format to request the data in
                    (defaults to DataFormat.ORIGINAL).
        :type fmt: :class:`DataFormat`

        :param json_fmt: The backup format to request in the event that the
                         `fmt` was DataFormat.ORIGINAL and the request returned
                         a JSON blob (happens for uploads from older mobile apps).
                         Using `DataFormat.ORIGINAL` will cause the JSON blob to
                         be returned.
                         (defaults to DataFormat.GPX)
        :type json_fmt: :class:`DataFormat`

        :return: A namedtuple with `filename` and `content` attributes:
                 - `filename` is the filename that Strava suggests for the file
                 - `contents` is an iterator that yields file contents as bytes
        :rtype: :class:`ExportFile`
        """
        __log__.debug("Getting data (in %s format) for activity %s", fmt, activity_id)

        fmt = DataFormat(fmt)
        json_fmt = DataFormat(json_fmt)
        resp = self.request_get(
            "activities/{}/export_{}".format(activity_id, fmt),
            stream=True,
            allow_redirects=False
        )

        # Gives a 302 back to the activity URL when trying to export a manual activity
        # TODO: Does this also happen with other errors?
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to download an activity"
                                      "".format(resp.status_code))

        # When downloading JSON, the Content-Type header will set to 'application/json'
        # If the json_fmt is not DataFormat.ORIGINAL, try the download again asking
        # for the json_fmt.
        if (fmt == DataFormat.ORIGINAL and json_fmt != fmt and
                resp.headers['Content-Type'].lower() == 'application/json'):
            return self.get_activity_data(activity_id, fmt=json_fmt, json_fmt=DataFormat.ORIGINAL)

        return self._make_export_file(resp, activity_id)

    def get_bike_details(self, bike_id):
        """
        Scrape the details of the specified bike

        :param bike_id: The id of the bike to retreive components for
                        (must start with a "b")
        :type bike_id: str
        """
        __log__.debug("Getting bike details for bike %s", bike_id)
        if not bike_id.startswith('b'):
            raise ValueError("Invalid bike id (must start with 'b')")

        resp = self.request_get(
            "bikes/{}".format(bike_id[1:]),  # chop off the leading "b"
            allow_redirects=False
        )
        if resp.status_code != 200:
            raise stravalib.exc.Fault(
                "Failed to load bike details page (status code: {})".format(resp.status_code),
            )

        soup = BeautifulSoup(resp.text, 'html5lib')

        ret = {}

        # Get data about the bike
        gear_table = soup.find("div", class_="gear-details").find("table")
        for k, v in zip(
                ["frame_type", "brand_name", "model_name", "weight"],
                [x.text for x in gear_table.find_all("td")][1::2]
        ):
            if not k:
                continue
            if k == "weight":
                # Strip non-number chars ("kg")
                # TODO: other units?
                v = float(NON_NUMBERS.sub('', v))
            elif k == "frame_type":
                v = FrameType.from_str(v)
            ret[k.lower()] = v

        # Get component data
        table = None
        for t in soup.find_all('table'):
            if t.find('thead'):
                table = t
                break
        else:
            raise ScrapingError(
                "Bike component table not found in the HTML - layout update?"
            )

        ret["components"] = []
        for row in table.tbody.find_all('tr'):
            cells = row.find_all('td')
            text = [cell.text.strip() for cell in cells]

            # Guard against "No active components" and other messages
            if len(cells) < 7:
                continue

            # Parse distance (convert to m from mi/km)
            mul = 1609.34708 if text[5].endswith("mi") else 1000
            distance = int(float(text[5].rstrip(" kmi").replace(",", "")) * mul)

            component_id = cells[6].find('a', text="Delete")['href'].rsplit("/", 1)[-1]

            ret["components"].append(ScrapedBikeComponent(
                id=component_id,
                type=text[0],
                brand_name=text[1],
                model_name=text[2],
                added=text[3],
                removed=text[4],
                distance=distance
            ))
        return ret

    def get_route_data(self, route_id, fmt=DataFormat.GPX):
        """
        Get a file containing the provided route's data

        The returned data can be either a GPX file, or a TCX file.

        :param route_id: The route to retrieve.
        :type route_id: int

        :param fmt: The format to request the data in. DataFormat.ORIGINAL is mapped to DataFormat.GPX
                    (defaults to DataFormat.GPX).
        :type fmt: :class:`DataFormat`

        :return: A namedtuple with `filename` and `content` attributes:
                 - `filename` is the filename that Strava suggests for the file
                 - `contents` is an iterator that yields file contents as bytes
        :rtype: :class:`ExportFile`
        """
        fmt = DataFormat.classify(DataFormat.GPX if fmt is DataFormat.ORIGINAL else fmt)
        url = "{}/routes/{}/export_{}".format(BASE_URL, route_id, fmt)
        resp = self._session.get(url, stream=True, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to download a route"
                                      "".format(resp.status_code))

        return self._make_export_file(resp, route_id)

    def get_all_bikes(self, athlete_id=None):
        """Scrape all bike information from Strava

        :yield: `ScrapedBike` objects
        """
        # Return minimal information from the athlete page if this isn't the
        # currently-logged in athlete.
        if int(athlete_id) != self.athlete_id:
            return self.get_athlete(athlete_id).bikes

        __log__.debug("Getting all bike data")
        resp = self.request_get("athletes/{}/gear/bikes".format(self.athlete_id))
        if not resp.ok:
            raise stravalib.exc.Fault("Failed to get list of bikes")
        try:
            yield from (
                ScrapedBike(
                    bind_client=self,
                    id="b{}".format(b.pop("id")),  # add "b" to gear id
                    **b
                )
                for b in resp.json()
            )
        except (TypeError, ValueError) as e:
            raise ScrapingError("Failed to parse bike data") from e

    def get_all_shoes(self, athlete_id=None):
        """Scrape all shoe information from Strava

        :yield: `ScrapedShoe` objects
        """
        # Return minimal information from the athlete page if this isn't the
        # currently-logged in athlete.
        if int(athlete_id) != self.athlete_id:
            return self.get_athlete(athlete_id).shoes

        __log__.debug("Getting all shoe data")
        resp = self.request_get("athletes/{}/gear/shoes".format(self.athlete_id))
        if not resp.ok:
            raise stravalib.exc.Fault("Failed to get list of shoes")
        try:
            yield from (ScrapedShoe(**s) for s in resp.json())
        except (TypeError, ValueError) as e:
            raise ScrapingError("Failed to parse shoe data") from e

    def get_all_gear(self):
        """Scrape all gear information from Strava

        :yield: `ScrapedBike` and `ScrapedShoe` objects
        """
        yield from self.get_all_bikes()
        yield from self.get_all_shoes()

    def get_gear(self, gear_id):
        """A scraping-based replacement for `stravalib.Client.get_gear`"""
        try:
            if gear_id.startswith("b"):
                return next(x for x in self.get_all_bikes() if x.id == gear_id)
            else:
                return next(x for x in self.get_all_shoes() if x.id == gear_id)
        except StopIteration:
            raise KeyError("No gear with id '{}' found".format(gear_id))

    def get_athlete(self, athlete_id=None):
        """A scraping-based replacement for `stravalib.Client.get_athlete`"""
        if athlete_id is None:
            athlete_id = self.athlete_id

        athlete_id = int(athlete_id)

        __log__.debug("Getting athlete %s", athlete_id)
        resp = self.request_get("athletes/{}".format(athlete_id))
        if not resp.ok:
            raise stravalib.exc.Fault("Failed to get athlete {}".format(athlete_id))

        ret = {
            "photos": [],
            "challenges": [],
        }
        soup = BeautifulSoup(resp.text, 'html5lib')

        for script in soup.find_all("script"):
            # This method only works on the currently-logged in athlete but
            # returns much more data.
            if athlete_id == self.athlete_id and "Strava.Models.CurrentAthlete" in script.text:
                m = ATHLETE_REGEX.search(script.text)
                if not m:
                    __log__.error("Failed to extract detailed athlete data")
                    continue
                try:
                    ret.update(json.loads(m.group(1)))
                except (TypeError, ValueError) as e:
                    __log__.error("Failed to parse extracted athlete data", exc_info=True)
                    continue

            elif "var trophiesAnalyticsProperties" in script.text:
                m = CHALLENGE_IDS_REGEX.search(script.text)
                if not m:
                    __log__.error("Failed to extract completed challenges")
                    continue
                try:
                    ret["challenges"] = json.loads(m.group(1))
                except (TypeError, ValueError) as e:
                    __log__.error("Failed to parse extracted challenge data", exc_info=True)
                    continue

            elif "var photosJson" in script.text:
                # Exact same as activity pages
                m = PHOTOS_REGEX.search(script.text)
                if not m:
                    __log__.error("Failed to extract photo data from page")
                    break
                try:
                    photos = json.loads(m.group(1))
                except (TypeError, ValueError) as e:
                    __log__.error("Failed to parse extracted photo data", exc_info=True)
                    break
                ret["photos"] = [ScrapedActivityPhoto(**p) for p in photos]

        # Failed the detailed scrape or not getting the currently-logged in athlete
        # (this method works for all athletes)
        if "id" not in ret:
            ret["id"] = athlete_id
            # There are multiple headings depending on the level of access
            for heading in soup.find_all("div", class_="profile-heading"):
                name = heading.find("h1", class_="athlete-name")
                if name:
                    ret["name"] = name.text.strip()

                location = heading.find("div", class_="location")
                if location:
                    ret["city"], ret["state"], ret["country"] = [x.strip() for x in location.text.split(",")]

                profile = heading.find("img", class_="avatar-img")
                if profile:
                    ret["profile"] = profile["src"]

        # Scrape basic gear info from the sidebar if not getting the logged
        # in athlete.
        # By providing minimal data for non-logged-in athletes, no more data
        # will be lazy-loaded by the bikes and shoes attributes. This is what
        # we want since the lazy-load would just call this function again.
        # However, when getting the logged in athlete's gear, we don't want to
        # set anything since the lazy-load will use the more detailed
        # get_all_bikes/gear functions instead of this one.
        if athlete_id != self.athlete_id:
            ret["bikes"] = []
            ret["shoes"] = []
            for gear in soup.select("div.section.stats.gear"):
                if "bikes" in gear["class"]:
                    type_ = "bikes"
                    cls = ScrapedBike
                elif "shoes" in gear["class"]:
                    type_ = "shoes"
                    cls = ScrapedShoe
                else:
                    continue

                for row in gear.find("table").find_all("tr"):
                    name, dist = row.find_all("td")
                    link=name.find("a")
                    gear_id = None
                    if link and type_ == "bikes":
                        gear_id = "b{}".format(link["href"].rsplit("/", 1)[-1])

                    ret[type_].append(cls(
                        id=gear_id,
                        name=name.text.strip(),
                        distance=int(float(NON_NUMBERS.sub('', dist.text.strip())) * 1000),
                    ))

        return ScrapedAthlete(bind_client=self, **ret)

    def get_challenge(self, challenge_id):
        """Get data about a challenge"""
        __log__.debug("Getting details for challenge %s", challenge_id)
        resp = self.request_get("challenges/{}".format(challenge_id))
        if not resp.ok:
            raise stravalib.exc.Fault("Failed to get challenge {}".format(challenge_id))

        data = {}
        soup = BeautifulSoup(resp.text, 'html5lib')
        react_data = soup.find("div", **{"data-react-class": "Show"})
        if react_data:
            # Extract data from the react version of the page
            data_str = html.unescape(
                react_data["data-react-props"]
                    .replace("&nbsp;", " ")
                    .replace("\n", "\\n")
            )
            try:
                data = json.loads(data_str)
            except (TypeError, ValueError) as e:
                raise ScrapingError("Failed to parse extracted challenge data") from e

            # Get the descript
            description_html = next(x for x in data["sections"] if x["title"] == "Overview")["content"][0]["text"].replace("&nbsp;", "")
            data["description"] = BeautifulSoup(description_html, 'html5lib').text
            data["name"] = data["header"]["name"]
            data["subtitle"] = data["header"]["subtitle"]
            data["teaser"] = data["summary"]["challenge"]["title"]
            data["badge_url"] = data["header"]["challengeLogoUrl"]
            data["share_url"] = "https://www.strava.com/challenges/{}".format(challenge_id)

            m = CHALLENGE_DATE_REGEX.search(data["summary"]["calendar"]["title"])
            if m:
                try:
                    data["start_date"], data["end_date"] = [
                        datetime.strptime(x, "%b %d, %Y") for x in m.groups()
                    ]
                except ValueError:
                    __log__.error("Failed to parse dates {}".format(m.groups()))
        else:
            # Look for the data in the older-style page
            for script in soup.find_all("script"):
                if "Strava.Models.Challenge" in script.text:
                    break
            else:
                raise ScrapingError("Failed to scrape challenge data {}".format(challenge_id))

            m = CHALLENGE_REGEX.search(script.text)
            if not m:
                raise ScrapingError("Failed to extract challenge data from page")

            data_str = html.unescape(m.group(1))
            try:
                data = json.loads(data_str)
            except (TypeError, ValueError) as e:
                raise ScrapingError("Failed to parse extracted challenge data") from e

            desc = soup.find("div", id="desc")
            if desc:
                data["description"] = desc.text

        data["id"] = challenge_id

        return ScrapedChallenge(**data)


class WebClient(stravalib.Client):
    """
    An extension to the stravalib Client that fills in some of the gaps in
    the official API using web scraping.

    Requires a JWT or both of email and password
    """

    def __new__(cls, *_, **__):
        self = super().__new__(cls)

        # Prepend some docstrings with the parent classes one
        for fcn in ("__init__", "get_gear", "get_athlete"):
            getattr(cls, fcn).__doc__ = getattr(super(), fcn).__doc__ + getattr(cls, fcn).__doc__

        # Delegate certain methods and properties to the scraper instance
        for fcn in ("delete_activity", "get_activity_data", "jwt", "csrf"):
            setattr(cls, fcn, cls._delegate(ScrapingClient, fcn))
        return self

    def __init__(self, *args, **kwargs):
        """
        :param email: The email of the account to log into
        :type email: str

        :param password: The password of the account to log into
        :type password: str

        :param jwt: The JWT of an existing session.
                    If not specified, email and password are required.
                    Can be accessed from the `.jwt` property.
        :type jwt: str

        :param csrf: A dict of the form: `{<csrf-param>: <csrf-token>}`.
                     If not provided, will be scraped from the about page.
                     Can be accessed from the `.csrf` property.
        :type csrf: dict
        """
        sc_kwargs = {
            k: kwargs.pop(k, None) for k in ("email", "password", "jwt", "csrf")
        }
        self._scraper = ScrapingClient(**sc_kwargs)
        super().__init__(*args, **kwargs)

        if self._scraper.athlete_id != self.get_athlete().id:
            raise ValueError("API and web credentials are for different accounts")

    def get_athlete(self, athlete_id=None):
        """
        Returned Athletes will have scraped attributes lazily added.
        Also, when accessing the bikes attribute, more scraped data will be available
        """
        athlete = super().get_athlete(athlete_id)
        # TODO: Should make the bind client this instance
        #       That way scraping/API functions can be mixed
        return Athlete(bind_client=self._scraper).from_object(athlete)

    def get_gear(self, gear_id):
        """
        Returned Bikes will have scraped attributes lazily added
        """
        gear = super().get_gear(gear_id)
        if isinstance(gear, _Bike):
            # TODO: Should make the bind client this instance
            #       That way scraping/API functions can be mixed
            return Bike(bind_client=self._scraper).from_object(gear)
        return gear

    def get_all_gear(self):
        """Get all gear information from Strava

        :yield: `stravalib.model.Bike` and `stravalib.model.Shoe` instances
        """
        athlete = self.get_athlete()
        if athlete.bikes is None and athlete.shoes is None:
            __log__.error("Failed to get gear data (missing profile:read_all scope?)")
            return

        for gear in athlete.bikes + athlete.shoes:
            yield self.get_gear(gear)

    @staticmethod
    def _delegate(clazz, name):
        func = getattr(clazz, name)
        is_prop = isinstance(func, property)

        @functools.wraps(func)
        def delegator(self, *args, **kwargs):
            if is_prop:
                return getattr(self._scraper, name)
            return getattr(self._scraper, name)(*args, **kwargs)

        if is_prop:
            delegator = property(delegator)
        return delegator
