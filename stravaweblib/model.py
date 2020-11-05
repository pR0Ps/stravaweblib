#!/usr/bin/env python

import enum
from datetime import date, datetime

from stravalib.attributes import (Attribute, DateAttribute, TimestampAttribute,
                                  TimeIntervalAttribute, LocationAttribute)
from stravalib.model import (BaseEntity, BoundEntity, LoadableEntity,
                             EntityCollection, Bike as _Bike)
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

    def __init__(self, attr, fcn=None, key=None):
        """Set up the LazyLoaded wrapper

        Can expand attributes individually using a lambda function (fcn), or
        multiple attributes at a time via an `expand` function defined on the
        class that houses it (key).

        Using `fcn`-based attributes is recommended when each attribute needs
        to be retrieved separately. Using `key`-based attributes is recommended
        when multiple attributes can be retrieved at the same time.

        :param attr: The `Attribute` to wrap (ie. `Attribute(int)`)
        :param fcn: This function will be called the first time the attribute
                    is requested. The result will be set as the attribute value.
        :param key: The key of the attribute in the lazyload cache. The lazyload
                    cache is stored on the parent class. When this attribute is
                    requested and the key in not in the cache, the `load_attribute`
                    function on the parent class is called and the result is
                    added to the cache. At this point, the key is poped out of
                    the cache and set as the attribute variable.
        """
        if not (bool(fcn) ^ bool(key)):
            raise ValueError("One of fcn or key (not both) is required")
        self._fcn = fcn
        self._key = key
        # Mimic the child Attribute's properties
        super().__init__(
            type_=attr.type,
            resource_states=attr.resource_states,
            units=attr.units
        )

    def __get__(self, obj, clazz):
        if obj is not None and obj not in self.data:
            if self._fcn:
                # Call the provided function to load the attribute
                value = self._fcn(obj)
            elif self._key:
                if not hasattr(obj, "_lazyload_cache"):
                    obj._lazyload_cache = {}

                # Use obj.load_attribute() to ensure the object is in the cache
                if self._key not in obj._lazyload_cache:
                    obj._lazyload_cache.update(obj.load_attribute(self._key))
                value = obj._lazyload_cache.pop(self._key)

            self.__set__(obj, value)
        return super().__get__(obj, clazz)


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


class _BikeData(LoadableEntity):
    """Mixin class to add weight and components to a Bike"""
    frame_type = LazyLoaded(Attribute(FrameType), key="frame_type")
    components = LazyLoaded(EntityCollection(ScrapedBikeComponent), key="components")
    weight = LazyLoaded(Attribute(float, units=uh.kg), key="weight")

    def load_attribute(self, _):
        """Expand the bike with more details using scraping"""
        self.assert_bind_client()

        d = self.bind_client.get_bike_details(self.id)
        # Upgrade the frame_type to the enum
        _dict_modify(d, "frame_type", "frame_type", fcn=lambda x: FrameType.from_str(x))
        return d

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


class Bike(_BikeData, _Bike) :
    __doc__ = _Bike.__doc__ + """
    Scraping adds weight and components attributes
    """


class ScrapedBike(ScrapedGear, _BikeData):
    """Represents a bike scraped from Strava

    The attributes are compatible with stravalib.models.Bike where they exist.
    """


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

    def load_attribute(self, _):
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
