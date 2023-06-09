from base64 import b64decode
import cgi
from collections import namedtuple
from datetime import date, datetime
import enum
import functools
import json
import time
from copy import copy
from json import JSONDecodeError
from typing import List, Union, Dict

from bs4 import BeautifulSoup
import requests
import stravalib


__all__ = ["WebClient", "FrameType", "DataFormat", "ExportFile", "ActivityFile"]

from pydantic import parse_obj_as

from .models import Kudos, MentionableAthlete, MentionableClub

BASE_URL = "https://www.strava.com"


ExportFile = namedtuple("ExportFile", ("filename", "content"))
ActivityFile = ExportFile  # TODO: deprecate and remove


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
    GRAVEL_BIKE = 5

    def __str__(self):
        return str(self.name).replace("_", " ").title()


class FeedType(str, enum.Enum):
    FOLLOWING = "following"
    CLUB = "club"
    MY_ACTIVITY = "my_activity"


class FollowsType(str, enum.Enum):
    FOLLOWING = "following"
    FOLLOWERS = "followers"
    SUGGESTED = "suggested"
    BOTH_FOLLOWING = "both_following"


class WebClient(stravalib.Client):
    """
    An extension to the stravalib Client that fills in some of the gaps in
    the official API using web scraping.
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
        elif email and password:
            self._login_with_password(email, password)
        else:
            raise ValueError("'jwt' or both of 'email' and 'password' are required")

        # Init the normal stravalib client with remaining args
        super().__init__(*args, **kwargs)

        # Verify that REST API and Web API correspond to the same Strava user account
        if self.access_token is not None:
            rest_id = str(self.get_athlete().id)
            web_id = self._session.cookies.get('strava_remember_id')
            if rest_id != web_id:
                raise stravalib.exc.LoginFailed("API and web credentials are for different accounts")
        else:
            # REST API does not have an access_token (yet). Should we verify the match after
            # exchange_code_for_token()?
            pass
        self.athlete_id = self._session.cookies.get('strava_remember_id')

    @property
    def jwt(self):
        return self._session.cookies.get('strava_remember_token')

    @property
    def csrf(self):
        if not self._csrf:
            self._csrf = self._get_csrf_token()
        return self._csrf

    def _get_csrf_token(self):
        """Get a CSRF token

        Uses the about page because it's small and doesn't redirect based
        on if the client is logged in or not.
        """
        login_html = self._session.get("{}/about".format(BASE_URL)).text
        soup = BeautifulSoup(login_html, 'html.parser')

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
        resp = self._session.post(
            "{}/session".format(BASE_URL),
            allow_redirects=False,
            data={
                "email": email,
                "password": password,
                "remember_me": "on",
                **self.csrf
            }
        )
        if not resp.is_redirect or resp.next.url == "{}/login".format(BASE_URL):
            raise stravalib.exc.LoginFailed("Couldn't log in to website, check creds")

    def delete_activity(self, activity_id):
        """
        Deletes the specified activity.

        :param activity_id: The activity to delete.
        :type activity_id: int
        """
        resp = self._session.post(
            "{}/activities/{}".format(BASE_URL, activity_id),
            allow_redirects=False,
            data={
                "_method": "delete",
                **self.csrf
            }
        )

        if not resp.is_redirect or resp.next.url != "{}/athlete/training".format(BASE_URL):
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

    def get_activity_data(self, activity_id, fmt=DataFormat.ORIGINAL,
                          json_fmt=None):
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
                         a JSON blob (happens for uploads from mobile apps).
                         Using `None` (default) will cause the JSON blob to be
                         returned.
        :type json_fmt: :class:`DataFormat` or None

        :return: A namedtuple with `filename` and `content` attributes:
                 - `filename` is the filename that Strava suggests for the file
                 - `contents` is an iterator that yields file contents as bytes
        :rtype: :class:`ExportFile`
        """
        fmt = DataFormat.classify(fmt)
        url = "{}/activities/{}/export_{}".format(BASE_URL, activity_id, fmt)
        resp = self._session.get(url, stream=True, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to download an activity"
                                      "".format(resp.status_code))

        # In the case of downloading JSON, the Content-Type header will
        # correctly be set to 'application/json'
        if (json_fmt and fmt == DataFormat.ORIGINAL and
                resp.headers['Content-Type'].lower() == 'application/json'):
            if json_fmt == DataFormat.ORIGINAL.value:
                raise ValueError("`json_fmt` parameter cannot be DataFormat.ORIGINAL")
            return self.get_activity_data(activity_id, fmt=json_fmt)

        return self._make_export_file(resp, activity_id)

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
    def _get_all_bike_components(self, bike_id):
        """
        Get all components for the specified bike

        :param bike_id: The id of the bike to retreive components for
                        (must start with a "b")
        :type bike_id: str
        """
        if not bike_id.startswith('b'):
            raise ValueError("Invalid bike id (must start with 'b')")

        # chop off the leading "b"
        url = "{}/bikes/{}".format(BASE_URL, bike_id[1:])

        resp = self._session.get(url, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault(
                "Failed to load bike details page (status code: {})".format(resp.status_code),
            )

        soup = BeautifulSoup(resp.text, 'html.parser')
        for table in soup.find_all('table'):
            if table.find('thead'):
                break
        else:
            raise ValueError("Bike component table not found in the HTML - layout update?")

        components = []
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

    def get_activity_kudos(self, activity_id: int) -> Kudos:
        """
        Get a list of athletes who kudoed a given activity.

        The returned data is more detailed information about the athlete in comparison with the API call.

        :param activity_id: a target activity ID for which to fetch kudos.
        :type activity_id: int

        :return: List of athletes who kudoed activity.
        :rtype: list
        """
        url = "{}/feed/activity/{}/kudos".format(BASE_URL, activity_id)
        resp = self._session.get(url, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to fetch detailed kudos."
                                      "".format(resp.status_code))

        return Kudos(**resp.json())

    def get_mentionable_entities(self) -> List[Union[MentionableAthlete, MentionableClub]]:
        """
        Get a list of mentionable entities.

        The returned data is a list of athletes and clubs that the authorised athlete is following.

        :return: List of mentionable entities.
        :rtype: list
        """
        url = "{}/athlete/mentionable_entities".format(BASE_URL)
        resp = self._session.get(url, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to fetch mentionable entities."
                                      "".format(resp.status_code))

        return parse_obj_as(List[Union[MentionableAthlete, MentionableClub]], resp.json())

    def give_kudos(self, activity_id: int) -> Dict:
        """
        Give kudos for a given activity.

        :param activity_id: a target activity ID to be kudoed.
        :type activity_id: int

        :return: Status: {"success":"true"}
        :rtype: dict
        """
        url = "{}/feed/activity/{}/kudo".format(BASE_URL, activity_id)
        resp = self._session.post(url, allow_redirects=True, data=self.csrf)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to give kudos for activity."
                                      "".format(resp.status_code))
        try:
            return resp.json()
        except JSONDecodeError:
            raise ValueError(resp.content)

    def post_comment(self,
                     activity_id: int,
                     comment: str,
                     mentionable_athlete_ids: List[int] = None,
                     mentionable_club_ids: List[int] = None
                     ):
        """
        Post a comment for a given activity.

        :param activity_id: a target activity ID to be commented.
        :type activity_id: int
        :param comment: a string to be posted.
        :type comment: str
        :param mentionable_athlete_ids: set ids of athletes to be mentioned, even one id should be passed as a list
        :type mentionable_athlete_ids: list
        :param mentionable_club_ids: set ids of clubs to be mentioned, even one id should be passed as a list
        :type mentionable_club_ids: list
        """
        athlete_str = ""
        club_str = ""
        if mentionable_athlete_ids:
            _athlete_str_pattern = "[strava://athletes/{}] "
            for athlete_id in mentionable_athlete_ids:
                athlete_str += copy(_athlete_str_pattern).format(athlete_id)

        if mentionable_club_ids:
            _club_str_pattern = "[strava://clubs/{}] "
            for club_id in mentionable_club_ids:
                club_str += copy(_club_str_pattern).format(club_id)

        comment = "{}{}{}".format(athlete_str, club_str, comment)

        url = "{}/feed/activity/{}/comment".format(BASE_URL, activity_id)
        resp = self._session.post(url,
                                  allow_redirects=True,
                                  data={
                                      "comment": comment,
                                      **self.csrf
                                  })
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to post comment for activity."
                                      "".format(resp.status_code))

    def like_comment(self, comment_id: int) -> Dict:
        """
        Give a like for a given comment.

        :param comment_id: a target comment ID to be liked.
        :type comment_id: int

        :return: Status: {"success":"true"}
        :rtype: dict
        """
        url = "{}/comments/{}/reactions".format(BASE_URL, comment_id)
        resp = self._session.post(url, allow_redirects=True, data=self.csrf)
        if resp.status_code != 201:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to like comment.".format(resp.status_code))
        try:
            return resp.json()
        except JSONDecodeError:
            raise ValueError(resp.content)

    def unlike_comment(self, comment_id: int):
        """
        Remove a like for a given comment.

        :param comment_id: a target comment ID to be unliked.
        :type comment_id: int
        """
        url = "{}/comments/{}/reactions".format(BASE_URL, comment_id)
        resp = self._session.post(url,
                                  allow_redirects=False,
                                  data={
                                      "_method": "delete",
                                      **self.csrf
                                  })
        if resp.status_code != 204:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to unlike comment.".format(resp.status_code))

    def _get_activity_feed(self,
                           feed_type: FeedType,
                           club_id: int = None,
                           athlete_id: int = None,
                           before: int = None,
                           cursor: int = None
                           ) -> List[dict]:
        """
        Return a list of feed entries of given feed type.

        :param feed_type: my_activity, following, club types are allowed
        :type feed_type: FeedType
        :param athlete_id: a target club ID
        :type athlete_id: int
        :param athlete_id: authorised athlete ID
        :type athlete_id: int
        :param before: an epoch timestamp to use for filtering activities that have taken place before a certain time
        :type before: int
        :param cursor: cursor of the last item in the previous page of results, used to request the subsequent page of results
        :type cursor: int

        :return: List of feed entries
        :rtype: List[dict]
        """
        entries = []

        if feed_type == FeedType.CLUB and club_id is None:
            raise ValueError("`club_id` param must be set.")

        url = "{}/dashboard/feed?feed_type={}".format(BASE_URL, feed_type)
        if feed_type == FeedType.CLUB:
            url = "{}&club_id={}".format(url, club_id)
        if athlete_id:
            url = "{}&athlete_id={}".format(url, athlete_id)
        if before:
            url = "{}&before={}".format(url, before)
        if cursor:
            url = "{}&cursor={}".format(url, cursor)

        resp = self._session.get(url, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault("Status code '{}' received when trying "
                                      "to retrieve dashboard feed."
                                      "".format(resp.status_code))

        try:
            resp = resp.json()
        except JSONDecodeError:
            raise ValueError(resp.content)

        entries.extend(resp["entries"])

        if resp.get("pagination").get("hasMore"):
            last_entry = entries[-1]
            cursor_data = last_entry["cursorData"]
            athlete_id = int(last_entry["viewingAthlete"]["id"])
            before = int(cursor_data["updated_at"])
            cursor = int(cursor_data["rank"]) if cursor_data.get("rank") else cursor_data.get("rank")
            entries.extend(self._get_activity_feed(feed_type=feed_type,
                                                   club_id=club_id,
                                                   athlete_id=athlete_id,
                                                   before=before,
                                                   cursor=cursor
                                                   )
                           )

        return entries

    def get_my_activity_feed(self) -> List[dict]:
        """
        Return a list of My Activity feed entries.

        :return: List of feed entries
        :rtype: List[dict]
        """
        return self._get_activity_feed(feed_type=FeedType.MY_ACTIVITY, athlete_id=self.athlete_id)

    def get_following_feed(self) -> List[dict]:
        """
        Return a list of the Following feed entries.

        :return: List of feed entries
        :rtype: List[dict]
        """
        return self._get_activity_feed(feed_type=FeedType.FOLLOWING, athlete_id=self.athlete_id)

    def get_club_feed(self, club_id: int) -> List[dict]:
        """
        Return a list of the given Club feed entries.

        :return: List of feed entries
        :rtype: List[dict]
        """
        return self._get_activity_feed(feed_type=FeedType.CLUB, club_id=club_id)

    def _parse_athlete_profile_following_tab(self,
                                             athlete_id: int,
                                             follows_type: FollowsType,
                                             pagination_url: str = None
                                             ) -> List[dict]:
        """
        Parse the profile following tab and return a list of athletes.

        :param athlete_id: a target athlete ID
        :type athlete_id: int
        :param follows_type: following, followers, suggested, both_following types are allowed
        :type follows_type: FollowsType
        :param pagination_url: parsing will process from the given pagination url
        :type pagination_url: str

        :return: List of athletes
        :rtype: List[dict]
        """

        if pagination_url:
            url = "{}{}".format(BASE_URL, pagination_url)
        else:
            url = "{}/athletes/{}/follows?type={}".format(BASE_URL, athlete_id, follows_type)

        resp = self._session.get(url, allow_redirects=False)
        if resp.status_code != 200:
            raise stravalib.exc.Fault(
                "Failed to load athlete profile page (status code: {})".format(resp.status_code),
            )

        soup = BeautifulSoup(resp.text, 'html.parser')
        tab_content = soup.find("div", attrs={"class": "tab-content"})
        list_athletes = tab_content.find("ul", attrs={"class": "list-athletes"})
        if not list_athletes:
            raise ValueError("Current athlete doesn't have any {}.".format(follows_type))

        athletes = []
        for athlete in list_athletes.find_all("li", recursive=False):
            athletes.append(
                {
                    "athlete_id": athlete.get("data-athlete-id"),
                    "athlete_name": athlete.find("div", attrs={"class": "text-callout"}).find("a").text.strip(),
                    "avatar_img": athlete.find("img", attrs={"class": "avatar-img"}).get("src"),
                    "location": athlete.find("div", attrs={"class": "location"}).text.strip()
                }
            )

        if tab_content.find("nav"):
            next_page = tab_content.find("li", attrs={"class": "next_page"}).find("a")
            if next_page:
                next_page_url = next_page.get("href")
                athletes.extend(self._parse_athlete_profile_following_tab(
                    athlete_id=athlete_id,
                    follows_type=follows_type,
                    pagination_url=next_page_url)
                )

        return athletes

    def get_followers_athletes(self, athlete_id: int = None) -> List[dict]:
        """
        Return a list of followers athletes of the authorised athlete or given athlete_id

        :param athlete_id: a target athlete ID
        :type athlete_id: int

        :return: List of followers athletes
        :rtype: List[dict]
        """
        return self._parse_athlete_profile_following_tab(athlete_id=athlete_id or self.athlete_id,
                                                         follows_type=FollowsType.FOLLOWERS)

    def get_following_athletes(self, athlete_id: int = None) -> List[dict]:
        """
        Return a list of following athletes of the authorised athlete or given athlete_id

        :param athlete_id: a target athlete ID
        :type athlete_id: int

        :return: List of following athletes
        :rtype: List[dict]
        """
        return self._parse_athlete_profile_following_tab(athlete_id=athlete_id or self.athlete_id,
                                                         follows_type=FollowsType.FOLLOWING)

    def get_suggested_athletes(self) -> List[dict]:
        """
        Return a list of suggested athletes of the authorised athlete.

        :return: List of suggested athletes
        :rtype: List[dict]
        """
        return self._parse_athlete_profile_following_tab(athlete_id=self.athlete_id, follows_type=FollowsType.SUGGESTED)

    def get_both_following_athletes(self, athlete_id: int) -> List[dict]:
        """
        Return a list of both following athletes of given athlete_id

        :param athlete_id: a target athlete ID
        :type athlete_id: int

        :return: List of following athletes
        :rtype: List[dict]
        """
        return self._parse_athlete_profile_following_tab(athlete_id=athlete_id, follows_type=FollowsType.BOTH_FOLLOWING)


# Inherit parent documentation for WebClient.__init__
WebClient.__init__.__doc__ = stravalib.Client.__init__.__doc__ + \
        """
        :param email: The email of the account to log into
        :type email: str

        :param password: The password of the account to log into
        :type password: str

        :param jwt: The JWT of an existing session.
                    If not specified, email and password are required.
        :type jwt: str

        :param csrf: A dict of the form: `{<csrf-param>: <csrf-token>}`.
                     If not provided, will be scraped from the about page.
                     Can be accessed from the `.csrf` property.
        :type csrf: dict
        """
