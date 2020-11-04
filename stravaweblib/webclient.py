#!/usr/bin/env python
from base64 import b64decode
import cgi
from collections import namedtuple
from datetime import datetime
import functools
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
                                ScrapedActivityPhoto, ScrapedAthlete)


__log__ = logging.getLogger(__name__)


# Used for filtering when scraping the activity list
ACTIVITY_WORKOUT_TYPES = {
    "Ride": {None: 10, "Race": 11, "Workout": 12},
    "Run": {None: 0, "Race": 1, "Long Run": 2, "Workout": 3}
}

# Regexes for pulling information out of the activity details page
PHOTOS_REGEX = re.compile(r"var photosJson\s*=\s*(\[.*\]);")
PAGE_VIEW_REGEX = re.compile(r"pageView\s*=\s*new\s+Strava.Labs.Activities.Pages.(\S+)PageView\([\"']?\d+[\"']?,\s*[\"']([^\"']+)")

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

    def get_bike_components(self, bike_id, on_date=None):
        """
        Get components for the specified bike

        :param bike_id: The id of the bike to retreive components for
                        (must start with a "b")
        :type bike_id: str

        :param on_date: Only return components on the bike for this day. If
                        `None`, return all components regardless of date.
        :type on_date: None or datetime.date or datetime.datetime
        """
        components = self._get_all_bike_components(bike_id)

        # Filter by the on_date param
        if on_date:
            if isinstance(on_date, datetime):
                on_date = on_date.date()
            return [c for c in components if \
                    (c['added'] or date.min) <= on_date <= (c['removed'] or date.max)]
        else:
            return components

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



class WebClient(stravalib.Client):
    """
    An extension to the stravalib Client that fills in some of the gaps in
    the official API using web scraping.

    Requires a JWT or both of email and password
    """

    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)

        # Prepend __init__'s docstring with the parent classes one
        cls.__init__.__doc__ = super().__init__.__doc__ + cls.__init__.__doc__

        # Delegate certain methods and properties to the scraper instance
        for fcn in ("delete_activity", "get_bike_components", "get_activity_data", "jwt", "csrf"):
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

    @staticmethod
    def _delegate(cls, name):
        func = getattr(cls, name)
        is_prop = isinstance(func, property)

        @functools.wraps(func)
        def delegator(self, *args, **kwargs):
            if is_prop:
                return getattr(self._scraper, name)
            return getattr(self._scraper, name)(*args, **kwargs)

        if is_prop:
            delegator = property(delegator)
        return delegator
