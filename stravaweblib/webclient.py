import cgi
from collections import namedtuple
from datetime import date, datetime
import functools
import enum
import re

import requests
import stravalib
from bs4 import BeautifulSoup


__all__ = ["WebClient", "FrameType", "DataFormat"]


BASE_URL = "https://www.strava.com"


ActivityFile = namedtuple("ActivityFile", ("filename", "content"))


class DataFormat(enum.Enum):
    ORIGINAL = "original"
    GPX = "gpx"
    TCX = "tcx"

    def __str__(self):
        return str(self.value)

    @classmethod
    def classify(cls, value):
        for x in cls:
            if x.value == str(value):
                return x
        raise ValueError("Invalid format '{}'".format(value))


class FrameType(enum.Enum):
    MOUNTAIN_BIKE = 1
    CROSS_BIKE = 2
    ROAD_BIKE = 3
    TIME_TRIAL_BIKE = 4

    def __str__(self):
        return str(self.name).replace("_", " ").title()


class WebClient(stravalib.Client):
    """
    An extension to the stravalib Client that fills in some of the gaps in
    the official API using web scraping.
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a new client object.

        :param access_token: The token that provides access to a specific Strava account.  If empty, assume that this
                             account is not yet authenticated.
        :type access_token: str

        :param rate_limit_requests: Whether to apply a rate limiter to the requests. (default True)
        :type rate_limit_requests: bool

        :param rate_limiter: A :class:`stravalib.util.limiter.RateLimiter' object to use.
                             If not specified (and rate_limit_requests is True), then
                             :class:`stravalib.util.limiter.DefaultRateLimiter' will
                             be used.
        :type rate_limiter: callable

        :param requests_session: (Optional) pass request session object.
        :type requests_session: requests.Session() object
        """
        email = kwargs.pop("email", None)
        password = kwargs.pop("password", None)
        if not email or not password:
            raise ValueError("'email' and 'password' kwargs are required")

        self._session = requests.Session()
        self._login(email, password)
        self._component_data = {}

        # Init the normal stravalib client with remaining args
        super().__init__(*args, **kwargs)

    def _login(self, email, password):
        """Log into the website"""

        login_url = "{}/login".format(BASE_URL)
        session_url = "{}/session".format(BASE_URL)

        # Get CSRF token
        login_html = self._session.get(login_url).text
        soup = BeautifulSoup(login_html, 'html5lib')

        try:
            head = soup.head
            csrf_param = head.find('meta', attrs={"name": "csrf-param"}).attrs['content']
            csrf_token = head.find('meta', attrs={"name": "csrf-token"}).attrs['content']
        except (AttributeError, KeyError):
            # "AttributeError: 'NoneType' object has no attr..." when failing
            # to find the tags.
            raise stravalib.exc.LoginFailed("Couldn't find CSRF token")

        post_info = {
            csrf_param: csrf_token,
            "email": email,
            "password": password,
            "remember_me": "on",
        }
        ret = self._session.post(session_url, data=post_info, allow_redirects=False)
        if ret.status_code != 302 or ret.headers['location'] == login_url:
            raise stravalib.exc.LoginFailed("Couldn't log in to website, check creds")

    def get_activity_data(self, activity, fmt=DataFormat.ORIGINAL,
                          json_fmt=None):
        """Get a file containing the provided activity's data

        The data can either be the original file that was uploaded, a GPX file,
        or a TCX file.

        The `fmt` param controls the format of the file. Accepted values are
        ('original', 'tcx', and 'gpx'). Defaults to 'original'.

        The `json_fmt` param controls the backup format to use if the requested
        format was 'original' and it returned a JSON blob (happens for uploads
        from the mobile apps). By default the JSON blob will be returned.
        """
        if isinstance(activity, stravalib.model.Activity):
            activity_id = activity.id
        elif isinstance(activity, (str, int)):
            activity_id = activity
        else:
            raise ValueError("Invalid `activity` parameter '{}'"
                             "".format(activity))

        fmt = DataFormat.classify(fmt)
        url = "{}/activities/{}/export_{}".format(BASE_URL, activity_id, fmt)
        resp = self._session.get(url, stream=True, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' recieved when trying "
                                      "to download an activity"
                                      "".format(resp.status_code))

        # In the case of downloading JSON, the Content-Type header will
        # correctly be set to 'application/json'
        if (json_fmt and fmt == DataFormat.ORIGINAL and
                resp.headers['Content-Type'].lower() == 'application/json'):
            if json_fmt == DataFormat.ORIGINAL.value:
                raise ValueError("`json_fmt` parameter cannot be 'original'")
            return self.get_activity_data(activity_id, fmt=json_fmt)


        # Get file name from request (if possible)
        content_disposition = resp.headers.get('content-disposition', "")
        filename = cgi.parse_header(content_disposition)[1].get('filename')

        # Sane default for filename
        if not filename:
            filename = str(activity_id)

        # Note that Strava always removes periods from the filename so if one
        # exists we know it's for the extension
        if "." not in filename:
            if fmt == DataFormat.ORIGINAL:
                ext = 'dat'
            else:
                ext = fmt
            filename = "{}.{}".format(filename, ext)

        # Return the filename and an iterator to download the file with
        return ActivityFile(filename=filename,
                            content=resp.iter_content(chunk_size=16384))

    def _parse_date(self, date_str):
        if not date_str:
            return None
        if date_str.lower() == "since beginning":
            # Different from no date, but don't know exactly when it was
            return datetime.utcfromtimestamp(0).date()
        try:
            return datetime.strptime(date_str, "%b %d, %Y").date()
        except ValueError as e:
            return None

    @functools.lru_cache()
    def _get_all_bike_components(self, bike):
        """Get all bike components"""

        if isinstance(bike, stravalib.model.Gear):
            bike = bike.id
        elif not isinstance(bike, str):
            raise ValueError("Invalid bike type (must be stravalib.model.Bike or str)")

        if not bike.startswith('b'):
            raise ValueError("Invalid bike id (must start with 'b')")

        # chop off the leading "b"
        url = "{}/bikes/{}".format(BASE_URL, bike[1:])

        resp = self._session.get(url, allow_redirects=False)
        if resp.status_code != 200:
            # TODO: Exception type
            raise Exception("Failed to load bike details page")

        soup = BeautifulSoup(resp.text, 'html5lib')
        for table in soup.find_all('table'):
            if table.find('thead'):
                break
        else:
            # TODO: Exception type
            raise Exception("Bike component table not found")

        components = []
        for row in table.tbody.find_all('tr'):
            cells = row.find_all('td')
            text = [cell.text.strip() for cell in cells]

            # Guard against "No active components" and other messages
            if len(cells) < 7:
                continue

            # Parse and convert from km to m
            # TODO: Will this ever be anything but km?
            distance = int(float(text[5].strip(" km").replace(',', '')) * 1000)

            component_id = cells[6].find('a', text="Delete")['href'].rsplit("/", 1)[-1]

            components.append({
                'id': component_id,
                'type': text[0],
                'brand': text[1],
                'model': text[2],
                'added': self._parse_date(text[3]),
                'removed': self._parse_date(text[4]),
                'distance': distance
            })
        return components

    def get_bike_components(self, bike, on_date=None):
        """Get the components for the specified bike

        If `on_date` is specified, only components on the bike on that date
        will be returned. It must be a `datetime.date` or `datetime.date`
        object.
        """
        components = self._get_all_bike_components(bike)

        # Filter by the on_date param
        if on_date:
            if isinstance(on_date, datetime):
                on_date = on_date.date()
            return [c for c in components if \
                    (c['added'] or date.min) <= on_date <= (c['removed'] or date.max)]
        else:
            return components
