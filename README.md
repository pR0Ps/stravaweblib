stravaweblib
============

Provides all the functionality of the [stravalib](https://github.com/hozn/stravalib) package and
extends it using web scraping.

Authentication
--------------
In order to log into the website, the `WebClient` class either needs an email and password, or the
[JWT](https://en.wikipedia.org/wiki/JSON_Web_Token) of an existing session. Strava stores this JWT
in the `strava_remember_token` cookie.

After the client has logged in, a JWT for the current session can be accessed via the `WebClient`'s
`jwt` property. Storing this JWT (and the `access_token` from `stravalib`) allows for resuming the
session without having to log in again. This can avoid rate limits and lockouts.

Example:
```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Store the current session's information
jwt = client.jwt
access_token = client.access_token

# Create a new client that continues to use the previous web session
client = WebClient(access_token=access_token, jwt=jwt)
```

Extra functionality
-------------------

### Export activities
Download activity files as GPX, TCX, or the original format they were uploaded in.

```python
from stravaweblib import WebClient, DataFormat

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get the first activity id (uses the normal stravalib API)
activities = client.get_activities()
activity_id = activities.next().id

# Get the filename and data stream for the activity data
data = client.get_activity_data(activity_id, fmt=DataFormat.ORIGINAL)

# Save the activity data to disk using the server-provided filename
with open(data.filename, 'wb') as f:
    f.writelines(data.content)
```

### Delete activities
Delete activities from the site. Note that this was previously possible via the API, but the
endpoint has been [removed as of 2017-01-17](https://developers.strava.com/docs/changelog/#january-17-2017).

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get the first activity id (uses the normal stravalib API)
activities = client.get_activities()
activity_id = activities.next().id

# Delete the activity
client.delete_activity(activity_id)
```

### Get bike components
Retrieve all components added to bikes. Can optionally only show components active at a certain date.

```python
from stravaweblib import WebClient
from datetime import datetime

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get a list of bikes the current user owns
athlete = client.get_athlete()
bikes = athlete.bikes

# Get the id of the first bike
bike_id = bikes.next().id

# Get all components of the first bike (past and present)
client.get_bike_components(bike_id)

# Get the current components on the first bike
client.get_bike_components(bike_id, on_date=datetime.now())
```

### Export routes
Download route files as GPX or TCX.

```python
from stravaweblib import WebClient, DataFormat

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get the first route id (uses the normal stravalib API)
routes = client.get_routes()
route_id = routes.next().id

# Get the filename and data stream for the activity data
data = client.get_route_data(route_id, fmt=DataFormat.GPX)

# Save the activity data to disk using the server-provided filename
with open(data.filename, 'wb') as f:
    f.writelines(data.content)
```

### Kudos of activity
Get a list of athletes who kudoed a given activity.
The returned data is more detailed information about the athlete in comparison with the API call.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get the id of the first activity of the current athlete
activities = client.get_activities()
activity_id = activities.next().id

# Get kudos data for activity of the current athlete
my_activity_kudos_data = client.get_activity_kudos(activity_id=activity_id)
    
# Get kudos data for other activities, it means we can retrieve kudos data of any activity
activity_id = 12345678
other_activity_kudos_data = client.get_activity_kudos(activity_id=activity_id)

```

### Mentionable entities
Get a list of mentionable entities.
The returned data is a list of athletes and clubs that the current athlete is following.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get all mentionable entities of the current athlete
mentionable_entities = client.get_mentionable_entities()
```

### Give kudos
Give kudos for a given activity.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Give kudos
activity_id = 12345678
client.give_kudos(activity_id=activity_id)
```

### Post comment
Post a comment for a given activity.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Post comment. Also, it is possible to mention somebody or any club
activity_id = 12345678
mentionable_athlete_ids = [12345, 12354] # optional
mentionable_club_ids = [1234] # optional
comment = "Wow, awesome result!"
client.post_comment(activity_id=activity_id,
                    mentionable_athlete_ids=mentionable_athlete_ids,
                    mentionable_club_ids=mentionable_club_ids,
                    comment=comment)
```
Example of the comment: **AthleteName1** **AthleteName2** **ClubName** Wow, awesome result!

### Like, unlike comment
Give or remove a like for a given comment.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Like comment
comment_id = 12345678
client.like_comment(comment_id=comment_id)

# Unlike comment
client.unlike_comment(comment_id=comment_id)
```

### Get feed entries
Return a list of feed entries.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Return a list of My Activity feed entries.
my_activity_feed_entries = client.get_my_activity_feed()

# Return a list of the Following feed entries.
following_feed_entries = client.get_following_feed()

# Return a list of the given Club feed entries.
club_id=1234
club_feed_entries = client.get_club_feed(club_id=club_id)
```

### Parse athlete profile following tab
Return a list of athletes.

```python
from stravaweblib import WebClient

# Log in (requires API token and email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Return a list of followers athletes of the authorised athlete or given athlete_id
my_followers = client.get_followers_athletes()

athlete_id=1234567
athlete_followers = client.get_followers_athletes(athlete_id=athlete_id)

# Return a list of following athletes of the authorised athlete or given athlete_id
my_following = client.get_following_athletes()

athlete_id=1234567
athlete_following = client.get_following_athletes(athlete_id=athlete_id)

# Return a list of suggested athletes of the authorised athlete.
suggested_athletes = client.get_suggested_athletes()

# Return a list of both following athletes. Both - it means the current and other athlete are following.
athlete_id=1234567
both_following_athletes = client.get_both_following_athletes(athlete_id=athlete_id)
```

License
=======
Licensed under the [Mozilla Public License, version 2.0](https://www.mozilla.org/en-US/MPL/2.0)
