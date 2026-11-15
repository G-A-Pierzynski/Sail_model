# SMS Weather and Marine Routing System

An SMS service that replies to text messages with marine weather forecasts and sailing route guidance. A user texts coordinates to a phone number and gets back a compact weather or route summary. The system can also store a user's location and proactively alert them when dangerous conditions are forecast.

It is built on AWS Lambda and Twilio, and uses the free Open-Meteo forecast API for all weather data. It was field-tested on two Baltic Sea sailing trips using Garmin inReach satellite devices.

## Architecture

The system is three separate AWS Lambda functions sitting behind a single Twilio webhook:

```
                 inbound SMS
                     |
                     v
            +-------------------+
   Twilio ->|      router       |   decides where the message goes
            +-------------------+
                 |          |
        has "->" |          | everything else
                 v          v
        +-----------+   +-----------+
        |   route   |   |  weather  |
        | (sailing  |   | (weather  |
        |  routes)  |   | + alerts) |
        +-----------+   +-----------+
                              |
                              v
                          DynamoDB
                      (saved locations)
```

The router function is the only one Twilio talks to. It inspects the message text and forwards the event to whichever service should handle it. The two services then send the reply directly to the user through the Twilio API.

## Repository layout

```
Sail_model/
  lambda_router.py      Twilio webhook target; dispatches to a service
  lambda_route.py       Sailing route forecasts with a vessel speed model
  lambda_weather.py     Point weather, saved locations, scheduled alerts
  requirements.txt      Python dependencies (pandas, twilio)
  README.md
```

Each handler is deployed as its own AWS Lambda function. Set each function's handler to match its filename, for example `lambda_router.lambda_handler`.

## router

The entry point. Twilio is configured to call this function on every inbound SMS.

It reads the message body and routes by a single rule:

- If the message contains `->`, it is treated as a route request and forwarded to the route Lambda.
- Anything else (bare coordinates or a command word) is forwarded to the weather Lambda.

It invokes the target function synchronously and returns that function's response to Twilio. On any error it returns an empty TwiML response, so Twilio does not retry or surface an error to the user. The router invokes its targets by their deployed Lambda names, `route` and `weather`.

## route

Handles sailing route requests. Triggered by the router when a message contains `->`.

### Input format

```
LAT,LON -> DEST_LAT,DEST_LON
```

Example (Tallinn to Stockholm):

```
59.444, 24.768 -> 59.329, 18.069
```

### What it does

1. Parses the start and destination coordinates.
2. Walks the great-circle route hour by hour (up to a 50-hour horizon), pulling the Open-Meteo forecast at each step.
3. Estimates boat speed at each step using a vessel model based on hull speed, true wind speed, and true wind angle. The default vessel is 23 m length and 5 m beam.
4. Builds a compact SMS containing the next several hours of wind along the route, an estimated speed over ground and ETA, and any hazard warnings.

### Reading the reply

The reply is abbreviated to fit SMS length limits. A typical reply looks like:

```
SOG~6.2kt ETA~14h
14h 18kt 245deg 24kt; 15h 20kt 250deg 27kt ...
```

- `14h 18kt 245deg 24kt` means: at 14:00, wind 18 knots from 245 degrees, gusting 24 knots.
- A trailing arrow shows the wind trend over the window: up arrow rising, down arrow falling.
- `V20` means the wind is veering (shifting clockwise) 20 degrees; `B20` means backing (shifting counter-clockwise).
- Rain tokens flag which upcoming hours have rain: `RL`, `RM`, `RS` are light, moderate, and heavy, followed by the hour offsets (for example `RM34` means moderate rain in hours 3 and 4).

### Hazard warnings

If conditions warrant, the reply is prefixed with a `WARN` line listing any of: gale tiers (`GALE`, `SGALE`, `STORM`, `VSTORM`, `HURR`), `SEV_GUSTS`, `SHIFT` (a significant wind shift with the time), `TS_RISK` (thunderstorm), `HEAVY_RAIN`, and `LOW_VIS` (visibility under 1 km).

## weather

Handles point weather lookups, remembers a user's location, and runs scheduled storm monitoring. Triggered by the router for any non-route message, and by a scheduled EventBridge rule for the monitoring sweep.

### Commands and input

| Message | Action |
|---|---|
| `59.444, 24.768` | Save this location and reply with current weather plus any warnings |
| `CHECK` / `STATUS` / `WEATHER` | Reply with weather for your last saved location |
| `START` / `SUBSCRIBE` / `MONITOR` | Enable proactive storm alerts for your saved location |
| `STOP` / `UNSUBSCRIBE` / `CANCEL` | Stop proactive alerts |

### Reading the reply

```
Wind: 18kt NE (21mph)
Temp: 7.4C/45F
Conditions normal
```

If there are hazards, the last line is replaced with up to two warnings such as `NOW: 35kt winds!`, `GALE in 4h`, a rapid wind increase, or heavy rain over the next 24 hours.

### Scheduled monitoring

When invoked by a scheduled event (event source `aws.events`), the function scans all users who have monitoring enabled, checks each saved location for dangerous conditions, and texts an alert to anyone at risk. Each user receives at most one alert every 6 hours.

### Location storage

Saved locations live in a DynamoDB table (default name `user-locations`, overridable with the `DYNAMODB_TABLE` environment variable). Records carry a 30-day TTL, so stale locations expire automatically.

## Configuration

### Environment variables

| Variable | router | route | weather |
|---|---|---|---|
| `TWILIO_ACCOUNT_SID` | - | required | required |
| `TWILIO_AUTH_TOKEN` | - | required | required |
| `TWILIO_PHONE_NUMBER` | - | required | required |
| `DYNAMODB_TABLE` | - | - | optional (default `user-locations`) |

The router needs no Twilio credentials; it only forwards events.

### AWS resources

- Three Lambda functions, deployed under the names `router`, `route`, and `weather`.
- A DynamoDB table for saved locations, with a `phone_number` partition key and a `ttl` attribute enabled for expiry.
- An IAM role for the router permitting `lambda:InvokeFunction` on the route and weather functions.
- An IAM role for the weather function permitting DynamoDB read/write.
- An EventBridge schedule that periodically invokes the weather function for the monitoring sweep.

### Dependencies

The route and weather functions import `pandas` and `twilio`, which are not in the default Lambda runtime; both are pinned in `requirements.txt`. Package them with the function as a deployment zip or a Lambda layer. `boto3`, used by weather, is already present in the Lambda runtime. The router has no third-party dependencies.

### Twilio setup

Point your Twilio number's incoming-message webhook at the router function's URL (via API Gateway or a Lambda function URL).

## Limitations

- Vessel dimensions in the route function are fixed defaults and cannot yet be set per user.
- The route function requests a fresh forecast for every simulated hour of the route, which can mean many API calls per request.
- Weather data comes from Open-Meteo and is provided as-is. This is an informational aid, not a substitute for official marine forecasts or navigational judgment.
