stravaweblib
============

Provides all the functionality of the [stravalib](https://github.com/hozn/stravalib) package and
extends it using web scraping.

Extra functionality
-------------------

### Export activities
Download activity files as GPX, TCX, or the original format they were uploaded in.

```python
from stravaweblib import WebClient

# Log in (requires actual email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get the first activity id (uses the normal stravalib API)
activities = client.get_activities()
activity_id = activities[0].id

# Get the filename and data stream for the activity data
data = client.get_activity_data(activity_id, fmt=DataFormat.ORIGINAL)

# Dump the activity data to disk
with open(data.filename, 'wb') as f:
    for chunk in data.content:
        if not chunk:
            break
        f.write(chunk)
```

### Get bike components
Retrieve all components added to bikes. Can optionally only show components active at a certain date.

```python
from stravaweblib import WebClient
from datetime import datetime

# Log in (requires actual email/password for the site)
client = WebClient(access_token=OAUTH_TOKEN, email=EMAIL, password=PASSWORD)

# Get a list of bike the current user owns
athlete = client.get_athlete()
bikes = athlete.bikes

# Get all components of the first bike (past and present)
client.get_bike_components(bikes[0])

# Get the current components on the first bike
client.get_bike_components(bikes[0], date=datetime.now())
```

License
=======
Licensed under the [Mozilla Public License, version 2.0](https://www.mozilla.org/en-US/MPL/2.0)
