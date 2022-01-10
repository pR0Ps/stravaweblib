#!/usr/bin/env python

import enum
from datetime import date, datetime

from stravalib.attributes import (Attribute, DateAttribute, TimestampAttribute,
                                  TimeIntervalAttribute, LocationAttribute)
from stravalib.model import (BaseEntity, BoundEntity, LoadableEntity as _LoadableEntity,
                             IdentifiableEntity, EntityCollection, EntityAttribute,
                             Athlete as _Athlete, Bike as _Bike)
from stravalib import unithelper as uh


def _parse_component_date(date_str):
    if not date_str:
        return None
    if date_str.lower() == "since beginning":
        # Different from no date, but don't know exactly when it was
        return datetime.utcfromtimestamp(0).date()
    try:
        return datetime.strptime(date_str, "%b %d, %Y").date()
    except ValueError:
        return None

def _decode_unicode_escapes(s):
    """Decodes unicode escapes (\xFFFF) enbeddded in a string"""
    return s.encode("utf-8").decode("unicode_escape")


def _dict_modify(d, prev, target, overwrite=True, default=None, fcn=None):
    """Translate the prev key to target

    Only non-None values will be set

    if overwrite is true, the target key will be overwritten even if something truthy is already there
    default controls if anything should be used if the prev key is not available
    l is a lambda function that the value will be passed through before being set.
    """
    if not overwrite and d.get(target):
        return

    t = d.pop(prev, default)
    if t is None:
        return
    if fcn:
        t = fcn(t)
    if t is None:
        return
    d[target] = t


class DataFormat(enum.Enum):
    ORIGINAL = "original"
    GPX = "gpx"
    TCX = "tcx"

    def __str__(self):
        return str(self.value)


class FrameType(enum.Enum):
    MOUNTAIN_BIKE = 1
    CROSS_BIKE = 2
    ROAD_BIKE = 3
    TIME_TRIAL_BIKE = 4

    def __str__(self):
        return str(self.name).replace("_", " ").title()

    @classmethod
    def from_str(cls, s):
        if isinstance(s, cls):
            return s
        return cls[s.replace(" ", "_").upper().replace("TT_", "TIME_TRIAL_")]


class MetaLazy(type):
    """A metaclass that returns subclasses of the class of the passed in Attribute

    This is used with the LazyLoaded class wrapper below to dynamically create
    lazy-loaded subclasses.

    Also, it names the returned types LazyLoaded<classname>
    """
    def __call__(cls, attr, *args, **kwargs):
        attr_cls = attr.__class__
        cls = cls.__class__(cls.__name__ + attr_cls.__name__, (cls, attr_cls), {})
        return super(MetaLazy, cls).__call__(attr, *args, **kwargs)


class LazyLoaded(metaclass=MetaLazy):
    """Class wrapper that handles lazy-loading an Attribute as it is requested"""

    def __init__(self, attr, *, fcn=None, key=None, property=False):
        """Set up the LazyLoaded wrapper

        Can expand attributes individually using a lambda function (fcn), or
        multiple attributes at a time via an `expand` function defined on the
        class that houses it (key).

        Using `fcn`-based attributes is recommended when each attribute needs
        to be retrieved separately. Using `key`-based attributes is recommended
        when multiple attributes can be retrieved at the same time.

        If `property` is True, the attribute will be loaded each time it is
        requested. This makes the attribute act more like a property.

        :param attr: The `Attribute` to wrap (ie. `Attribute(int)`)
        :param fcn: This function will be called the first time the attribute
                    is requested. The result will be set as the attribute value.
        :param key: The key of the attribute in the lazyload cache. The lazyload
                    cache is stored on the parent class. When this attribute is
                    requested and the key in not in the cache, the `load_attribute`
                    function on the parent class is called and the result is
                    added to the cache. Any future accesses will return the value
                    from the cache. If the key is not in the cache, `None` is
                    returned.
        :param property: Don't store the result of the lazy load

        Special cases:
         - If a lazy-loaded attribute is None, lazy-loading will be attempted
           each time it is accessed. This allows for null values to be updated
           with new data.
         - If the load_attribute function returns None for a property, it will
           not be attempted again.

        """
        if not (bool(fcn) ^ bool(key)):
            raise ValueError("One of fcn or key (not both) is required")
        self._property = property
        self._fcn = fcn
        self._key = key
        # Mimic the child Attribute's properties
        super().__init__(
            type_=attr.type,
            resource_states=attr.resource_states,
            units=attr.units
        )

    def __get__(self, obj, clazz):
        if obj is None or not (self._property or self.data.get(obj) is None):
            return super().__get__(obj, clazz)

        if self._fcn:
            # Call the provided function to load the attribute
            value = self._fcn(obj)
            if value is not None and not self._property:
                self.__set__(obj, value)
            return value
        elif self._key:
            if not hasattr(obj, "_lazyload_cache"):
                obj._lazyload_cache = {}

            # Use obj.load_attribute() to ensure the object is in the cache
            if self._key not in obj._lazyload_cache:
                obj._lazyload_cache.update(obj.load_attribute(self._key) or {})

            # Don't set it on the object, keep accessing out of the cache
            return obj._lazyload_cache.get(self._key, None)

        raise AssertionError("No fcn or key?")

    def __set__(self, obj, val):
        if self._property:
            raise AttributeError(
                "Can't set {} property on {!r}".format(self.__class__.__name__, obj)
            )
        super().__set__(obj, val)


# TODO: probably delete this
class LoadableEntity(_LoadableEntity):

    def load_attribute(self, key):
        return {}


class ScrapedGear(BaseEntity):
    """Represents gear scraped from Strava

    The attributes are compatible with stravalib.model.Gear where they exist
    """
    id = Attribute(str)
    name = Attribute(str)
    distance = Attribute(float, units=uh.meters)
    primary = Attribute(bool)
    brand_name = Attribute(str)
    model_name = Attribute(str)
    description = Attribute(str)

    def from_dict(self, d):
        _dict_modify(d, "display_name", "name", overwrite=False)
        _dict_modify(d, "default", "primary", overwrite=False)
        _dict_modify(d, "total_distance", "distance", overwrite=False,
                     fcn=lambda x: float(x.replace(",", "")) * 1000)

        return super().from_dict(d)

    def __repr__(self):
        return "<{} id={} name={!r}>".format(
            self.__class__.__name__,
            self.id,
            self.name
        )


class ScrapedShoe(ScrapedGear):
    """Represents a pair of shoes scraped from Strava

    The attributes are compatible with stravalib.model.Shoe where they exist
    """
    pass


class ScrapedBikeComponent(BaseEntity):
    """Represents a bike component scraped from Strava"""

    id = Attribute(int)
    type = Attribute(str)
    brand_name = Attribute(str)
    model_name = Attribute(str)
    added = DateAttribute()
    removed = DateAttribute()
    distance = Attribute(int, units=uh.meters)

    def from_dict(self, d):
        # Parse and convert dates into something DateAttribute can understand
        _dict_modify(d, "added", "added", fcn=_parse_component_date)
        _dict_modify(d, "removed", "removed", fcn=_parse_component_date)

        return super().from_dict(d)

    def __repr__(self):
        return "<{} id={} type={!r}>".format(
            self.__class__.__name__,
            self.id,
            self.type
        )


class _ScrapedBikeData(LoadableEntity):
    """Mixin class to add weight and components to a Bike"""

    components = LazyLoaded(EntityCollection(ScrapedBikeComponent), key="components")
    weight = LazyLoaded(Attribute(float, units=uh.kg), key="weight")

    def load_attribute(self, key):
        """Expand the bike with more details using scraping"""
        self.assert_bind_client()
        return self.bind_client.get_bike_details(self.id)

    def components_on_date(self, on_date):
        """Get bike components installed on the specified date

        :type on_date: None or datetime.date or datetime.datetime
                       (datetimes will lose time-precision)
        """
        if on_date is None:
            return self.components

        if isinstance(on_date, datetime):
            on_date = on_date.date()

        return [
            c for c in self.components
            if (c.added or date.min) <= on_date <= (c.removed or date.max)
        ]


class Bike(_ScrapedBikeData, _Bike) :
    __doc__ = _Bike.__doc__ + """
    Scraping adds weight and components attributes
    """

    def from_object(self, b):
        self.from_dict(b.to_dict())
        return self


class ScrapedBike(ScrapedGear, _ScrapedBikeData):
    """Represents a bike scraped from Strava

    The attributes are compatible with stravalib.models.Bike where they exist.
    """
    # NOTE: These are here to take advantage of the load_attributes function
    #       of the _ScrapedBikeData class in case the ScrapedBike was
    #       constructed from a regular bike without the attributes set.
    frame_type = LazyLoaded(Attribute(FrameType), key="frame_type")
    brand_name = LazyLoaded(Attribute(str), key="brand_name")
    model_name = LazyLoaded(Attribute(str), key="model_name")
    description = LazyLoaded(Attribute(str), key="description")


class ScrapedActivityPhoto(BaseEntity):
    """Represents a photo scraped from Strava's activity details page

    The attributes are compatible with stravalib.models.ActivityPhoto where
    they exist.
    """

    unique_id = Attribute(str)
    activity_id = Attribute(int)
    athlete_id = Attribute(int)
    caption = Attribute(str)

    location = LocationAttribute()

    urls = Attribute(dict) # dimension: url

    def from_dict(self, d):
        _dict_modify(d, "photo_id", "unique_id")
        _dict_modify(d, "owner_id", "athlete_id")

        # The caption has unicode escapes (ie. \uFFFF) embedded in the string
        _dict_modify(d, "caption_escaped", "caption", fcn=_decode_unicode_escapes)

        if "dimensions" in d:
            d["urls"] = {
                str(min(dim.values())): d.pop(name)
                for name, dim in d.pop("dimensions").items()
            }
        if "lat" in d and "lng" in d:
            d["location"] = [d.pop("lat"), d.pop("lng")]

        return super().from_dict(d)


class ScrapedActivity(LoadableEntity):
    """
    Represents an Activity (ride, run, etc.) that was scraped from the website

    The attributes are compatible with stravalib.model.Activity where they exist
    """

    name = Attribute(str)
    description = Attribute(str)
    type = Attribute(str)
    workout_type = Attribute(str)

    start_date = TimestampAttribute()
    distance = Attribute(float)
    moving_time = TimeIntervalAttribute()
    elapsed_time = TimeIntervalAttribute()
    total_elevation_gain = Attribute(float)
    suffer_score = Attribute(int)
    calories = Attribute(float)
    gear_id = Attribute(str)

    # True if the activity has GPS coordinates
    # False for trainers, manual activities, etc
    has_latlng = Attribute(bool)

    trainer = Attribute(bool)
    commute = Attribute(bool)
    private = Attribute(bool)
    flagged = Attribute(bool)

    manual = LazyLoaded(Attribute(bool), key="manual")
    photos = LazyLoaded(EntityCollection(ScrapedActivityPhoto), key="photos")
    device_name = LazyLoaded(Attribute(str), key="device_name")

    def load_attribute(self, key):
        if key not in {"manual", "photos", "device_name"}:
            return super().load_attribute(key)

        self.assert_bind_client()
        return self.bind_client.get_extra_activity_details(self.id)

    @property
    def total_photo_count(self):
        return len(self.photos)

    def from_dict(self, d):
        # Only 1 of these will set the gear_id
        _dict_modify(d, "bike_id", "gear_id", fcn=lambda x: "b{}".format(x))
        _dict_modify(d, "athlete_gear_id", "gear_id", fcn=lambda x: "g{}".format(x))

        _dict_modify(d, "start_time", "start_date")
        _dict_modify(d, "distance_raw", "distance")
        _dict_modify(d, "moving_time_raw", "moving_time")
        _dict_modify(d, "elapsed_time_raw", "elapsed_time")
        _dict_modify(d, "elevation_gain_raw", "elevation_gain")

        return super().from_dict(d)


class ScrapedChallenge(IdentifiableEntity):

    url = Attribute(str)
    name = Attribute(str)
    subtitle = Attribute(str)
    teaser = Attribute(str)
    overview = Attribute(str)
    badge_url = Attribute(str)

    start_date = TimestampAttribute()
    end_date = TimestampAttribute()

    def trophy_url(self, percent_complete=100):
        """Return a url for a trophy image for the percentage complete

        Note that not all challenges have images for all percentages. Using
        100 should always work.
        """
        if not self.badge_url:
            return
        base, ext = self.badge_url.rsplit(".", 1)
        return "{}-{}.{}".format(base, percent_complete, ext)

    def from_dict(self, d):
        #_dict_modify(d, "title", "name")
        _dict_modify(d, "description", "overview")
        _dict_modify(d, "url", "badge_url")
        _dict_modify(d, "share_url", "url")
        return super().from_dict(d)


class _AthleteData(LoadableEntity):
    """Mixin class to add photos, challenges, and a name to an Athlete"""
    photos = LazyLoaded(EntityCollection(ScrapedActivityPhoto), key="photos")
    challenges = LazyLoaded(Attribute(list), key="challenges")
    bikes = LazyLoaded(EntityCollection(ScrapedBike), key="bikes")
    shoes = LazyLoaded(EntityCollection(ScrapedShoe), key="shoes")

    # Dynamically compute the display name in the same way Strava does
    name = LazyLoaded(
        Attribute(str),
        fcn=lambda x: "{} {}".format(x.firstname or "", x.lastname or "").strip(),
        property=True
    )

    def load_attribute(self, key):
        self.assert_bind_client()

        # TODO: bikes and shoes only returns scraping-based data
        if key == "bikes":
            return {"bikes": self.bind_client.get_all_bikes(self.id)}
        elif key == "shoes":
            return {"shoes": self.bind_client.get_all_shoes(self.id)}
        elif key in {"photos", "challenges"}:
            d = self.bind_client.get_athlete(self.id)
            return {
                "photos": d.photos,
                "challenges": d.challenges,
            }
        else:
            return super().load_attribute(key)


class Athlete(_AthleteData, _Athlete):
    __doc__ = _Athlete.__doc__ + """
    Scraping adds photos, challenges, and name attributes
    """
    def from_object(self, a):
        self.from_dict(a.to_dict())
        return self


class ScrapedAthlete(_AthleteData):
    """
    Represents Athlete data scraped from the website

    The attributes are compatible with stravalib.model.Athlete where they exist
    """
    firstname = Attribute(str)
    lastname = Attribute(str)

    profile = Attribute(str)
    city = Attribute(str)
    state = Attribute(str)
    country = Attribute(str)
    location = LocationAttribute()

    def from_dict(self, d):
        # Merge geo subdict into the main dict
        d.update(d.pop("geo", {}))

        _dict_modify(d, "photo", "profile_medium")
        _dict_modify(d, "photo_large", "profile")
        _dict_modify(d, "first_name", "firstname")
        _dict_modify(d, "last_name", "lastname")
        _dict_modify(d, "gender", "sex")
        _dict_modify(d, "lat_lng", "location")

        # According to some code returned in the HTML, Strava computes the
        # display name using "<first> <last>". He we make an attempt to break
        # the display name back up into it's parts. This is only for
        # compatibility with the stravalib API - you should always use obj.name
        name = d.pop("name", None)
        if name and "firstname" not in d and "lastname" not in d:
            # total guess: assume more last names have spaces than first
            d["firstname"], d["lastname"] = name.split(" ", 1)

        return super().from_dict(d)
