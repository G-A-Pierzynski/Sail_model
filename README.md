# SMS Weather and Marine Routing System

An SMS service that replies to text messages with marine weather forecasts and
sailing route guidance. A user texts coordinates to a phone number and gets back
a compact weather or route summary. The system can also store a user's location
and proactively alert them when dangerous conditions are forecast.

It is built on AWS Lambda + Twilio, and uses the free
[Open-Meteo](https://open-meteo.com/) forecast API for all weather data.

## Architecture

The system is three separate AWS Lambda functions sitting behind a single Twilio
webhook:

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
   +---------------------+  +-----------------------+
   |  route-sms-service  |  |  weather-sms-service  |
   |  (sailing routes)   |  |  (weather + alerts)   |
   +---------------------+  +-----------------------+
                                       |
                                       v
                                  DynamoDB
                              (saved locations)
```

The router is the only function Twilio talks to. It inspects the message text
and forwards the event to whichever service should handle it. The two services
then send the reply directly to the user through the Twilio API.

## Repository layout

```
sms-weather-system/
  router/
    lambda_function.py        Twilio webhook target; dispatches to a service
  route-sms-service/
    lambda_function.py        Sailing route forecasts with a vessel speed model
  weather-sms-service/
    lambda_function.py        Point weather, saved locations, scheduled alerts
  README.md
  .gitignore
```

---

## router

The entry point. Twilio is configured to call this function on every inbound SMS.

It reads the message body and routes by a single rule:

- If the message contains `->` it is treated as a route request and forwarded to
  the `route-sms-service` Lambda.
- Anything else (bare coordinates or a command word) is forwarded to the
  `weather-sms-service` Lambda.

It invokes the target function synchronously (`RequestResponse`) and returns that
function's response back to Twilio. On any error it returns an empty TwiML
response so Twilio does not retry or surface an error to the user.

Important: the target names `route-sms-service` and `weather-sms-service` are
hardcoded in this file. The two service Lambdas must be deployed under exactly
those function names, or the invoke call will fail.

---

## route-sms-service

Handles sailing route requests. Triggered by the router when a message contains
`->`.

### Input format

```
LAT,LON -> DEST_LAT,DEST_LON
```

Example:

```
59.444, 24.768 -> 59.329, 18.069
```

(Tallinn to Stockholm.)

### What it does

1. Parses the start and destination coordinates.
2. Walks the great-circle route hour by hour (up to a 50-hour horizon), pulling
   the Open-Meteo forecast at each step.
3. Estimates boat speed at each step using a simple vessel model based on hull
   speed, true wind speed, and true wind angle. Default vessel is 23 m length,
   5 m beam (hardcoded in the handler).
4. Builds a compact SMS containing the next several hours of wind along the
   route, an estimated speed over ground and ETA, and any hazard warnings.

### Reading the reply

The reply is abbreviated to fit inside SMS length limits. A typical line looks
like:

```
SOG~6.2kt ETA~14h
14h 18kt 245° 24kt; 15h 20kt 250° 27kt ...
```

- `14h 18kt 245° 24kt` means: at 14:00, wind 18 knots from 245 degrees, gusting
  24 knots.
- A trailing arrow shows the wind trend over the window: up arrow rising,
  down arrow falling.
- `V20°` means the wind is veering (shifting clockwise) 20 degrees;
  `B20°` means backing (shifting counter-clockwise).
- Rain tokens flag which upcoming hours have rain: `RL`, `RM`, `RS` are
  light / moderate / heavy, followed by the hour offsets (for example `RM34`
  means moderate rain in hours 3 and 4).

### Hazard warnings

If conditions warrant, the reply is prefixed with a `WARN` line listing any of:
gale tiers (`GALE`, `SGALE`, `STORM`, `VSTORM`, `HURR`), `SEV_GUSTS`,
`SHIFT` (a significant wind shift with the time), `TS_RISK` (thunderstorm),
`HEAVY_RAIN`, and `LOW_VIS` (visibility under 1 km).

---

## weather-sms-service

Handles point weather lookups, remembers a user's location, and runs scheduled
storm monitoring. Triggered by the router for any non-route message, and also by
a scheduled EventBridge rule for the monitoring sweep.

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
Temp: 7.4°C/45°F
Conditions normal
```

If there are hazards, the last line is replaced with up to two warnings such as
`NOW: 35kt winds!`, `GALE in 4h`, a rapid wind increase, or heavy rain over the
next 24 hours.

### Scheduled monitoring

When invoked by a scheduled event (event `source` of `aws.events`), the function
scans all users who have monitoring enabled, checks each saved location for
dangerous conditions, and texts an alert to anyone at risk. Each user is
rate-limited to at most one alert every 6 hours so they are not spammed.

### Location storage

Saved locations live in a DynamoDB table (default name `user-locations`,
overridable with the `DYNAMODB_TABLE` environment variable). Records carry a
30-day TTL, so stale locations expire automatically.

---

## Configuration

### Environment variables

All three functions need the Twilio credentials. The weather service
additionally uses the optional table name.

| Variable | router | route-sms-service | weather-sms-service |
|---|---|---|---|
| `TWILIO_ACCOUNT_SID` | - | required | required |
| `TWILIO_AUTH_TOKEN` | - | required | required |
| `TWILIO_PHONE_NUMBER` | - | required | required |
| `DYNAMODB_TABLE` | - | - | optional (default `user-locations`) |

The router itself needs no Twilio credentials; it only forwards events.

### AWS resources

- Three Lambda functions, deployed under the names `router`,
  `route-sms-service`, and `weather-sms-service` (the latter two names must match
  exactly, since the router calls them by name).
- A DynamoDB table for saved locations, with a `phone_number` partition key and
  a `ttl` attribute enabled for expiry.
- An IAM role for the router permitting `lambda:InvokeFunction` on the two
  services.
- An IAM role for the weather service permitting DynamoDB read/write.
- An EventBridge (CloudWatch Events) schedule that periodically invokes the
  weather service for the monitoring sweep, if you want proactive alerts.

### Dependencies

Both services import `pandas` and `twilio`, which are not in the default Lambda
runtime. Package them with the function (a deployment zip or a Lambda layer).
The router has no third-party dependencies.

### Twilio setup

Point your Twilio number's incoming-message webhook at the `router` function's
URL (via API Gateway or a Lambda function URL).

## Notes and limitations

- The vessel dimensions in the route service are hardcoded defaults; there is no
  way to set them per user yet.
- The route service requests a fresh forecast for every simulated hour of the
  route, which can mean many API calls per request. Fine for light use; revisit
  if you hit Open-Meteo rate limits.
- Weather data comes from Open-Meteo and is provided as-is. This is an
  informational aid, not a substitute for official marine forecasts or
  navigational judgment.
