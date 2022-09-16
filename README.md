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


License
=======
Licensed under the [Mozilla Public License, version 2.0](https://www.mozilla.org/en-US/MPL/2.0)
