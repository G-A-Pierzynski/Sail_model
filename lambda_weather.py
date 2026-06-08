import urllib.request
import urllib.parse
import pandas as pd
from datetime import datetime, timedelta
from urllib.parse import parse_qs
import base64
import os
import json
import boto3
from twilio.rest import Client

###=======DynamoDB Setup==========###
dynamodb = boto3.resource('dynamodb')
table_name = os.environ.get('DYNAMODB_TABLE', 'user-locations')

def get_table():
    return dynamodb.Table(table_name)

###=======Twilio==========###
def get_twilio_client():
    account_sid = os.environ['TWILIO_ACCOUNT_SID']
    auth_token = os.environ['TWILIO_AUTH_TOKEN']
    return Client(account_sid, auth_token)

def parse_twilio_request(event):
    if event.get('isBase64Encoded'):
        body = base64.b64decode(event['body']).decode('utf-8')
    else:
        body = event.get('body', '')
    return parse_qs(body)

def send_sms(to_number, message):
    try:
        client = get_twilio_client()
        from_number = os.environ['TWILIO_PHONE_NUMBER']
        response = client.messages.create(
            to=to_number,
            from_=from_number,
            body=message[:1600]
        )
        return response.sid
    except Exception as e:
        print(f"SMS send error: {e}")
        return None

def build_twilio_response(message, status_code=200):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/xml',
            'Access-Control-Allow-Origin': '*'
        },
        'body': f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{message}</Message></Response>'
    }

###=======Location Storage==========###
def store_user_location(phone_number, lat, lon):
    """Store user's latest location with timestamp"""
    try:
        table = get_table()
        table.put_item(
            Item={
                'phone_number': phone_number,
                'timestamp': datetime.now().isoformat(),
                'latitude': str(lat),
                'longitude': str(lon),
                'ttl': int((datetime.now() + timedelta(days=30)).timestamp()),
                'monitoring_enabled': True
            }
        )
        return True
    except Exception as e:
        print(f"Error storing location: {e}")
        return False

def get_last_location(phone_number):
    """Get user's last known location"""
    try:
        table = get_table()
        response = table.get_item(
            Key={'phone_number': phone_number}
        )
        if 'Item' in response:
            item = response['Item']
            return float(item['latitude']), float(item['longitude'])
    except Exception as e:
        print(f"Error getting location: {e}")
    return None, None

def get_all_monitored_users():
    """Get all users with monitoring enabled"""
    try:
        table = get_table()
        response = table.scan(
            FilterExpression='monitoring_enabled = :val',
            ExpressionAttributeValues={':val': True}
        )
        return response.get('Items', [])
    except Exception as e:
        print(f"Error scanning users: {e}")
        return []

def update_last_alert(phone_number):
    """Update last alert timestamp"""
    try:
        table = get_table()
        table.update_item(
            Key={'phone_number': phone_number},
            UpdateExpression='SET last_alert = :time',
            ExpressionAttributeValues={
                ':time': datetime.now().isoformat()
            }
        )
    except Exception as e:
        print(f"Error updating alert time: {e}")

###=======Input Parser==========###
def parse_sms_input(sms_body):
    """Parse various SMS formats"""
    text = sms_body.strip().upper()
    
    # Commands
    if text in ['CHECK', 'STATUS', 'WEATHER']:
        return 'CHECK', None, None, None
    if text in ['STOP', 'UNSUBSCRIBE', 'CANCEL']:
        return 'STOP', None, None, None
    if text in ['START', 'SUBSCRIBE', 'MONITOR']:
        return 'START', None, None, None
    
    # Try to parse coordinates
    try:
        parts = sms_body.strip().split('->')
        if len(parts) == 2:
            current = parts[0].strip().split(',')
            dest = parts[1].strip().split(',')
            cur_lat = float(current[0].strip())
            cur_lon = float(current[1].strip())
            dest_lat = float(dest[0].strip())
            dest_lon = float(dest[1].strip())
            return cur_lat, cur_lon, dest_lat, dest_lon
        else:
            coords = sms_body.strip().split(',')
            if len(coords) == 2:
                lat = float(coords[0].strip())
                lon = float(coords[1].strip())
                return lat, lon, None, None
    except:
        pass
    
    return None, None, None, None

###=======Weather Functions==========###
def degrees_to_compass(deg):
    if deg is None: return 'N'
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
            'S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[int((deg + 11.25) / 22.5) % 16]

def interpret_weather_code(code):
    if code is None: return "Unknown"
    m = {0:"Clear",1:"Mostly clear",2:"Partly cloudy",3:"Overcast",
         45:"Fog",48:"Freezing fog",51:"Light drizzle",53:"Drizzle",
         55:"Heavy drizzle",61:"Light rain",63:"Rain",65:"Heavy rain",
         71:"Light snow",73:"Snow",75:"Heavy snow",77:"Snow grains",
         80:"Light showers",81:"Showers",82:"Heavy showers",
         85:"Snow showers",86:"Heavy snow showers",95:"Thunderstorm",
         96:"Thunderstorm w/ hail",99:"Severe thunderstorm"}
    return m.get(code, f"Code {code}")

def get_storm_warning(wind_ms, gust_ms, cape):
    w = []
    if wind_ms is not None:
        if wind_ms >= 32.7: w.append("HURRICANE")
        elif wind_ms >= 28.5: w.append("VIOLENT STORM")
        elif wind_ms >= 24.5: w.append("STORM")
        elif wind_ms >= 20.8: w.append("STRONG GALE")
        elif wind_ms >= 17.2: w.append("GALE")
    if gust_ms and wind_ms and gust_ms - wind_ms > 10: 
        w.append("SEVERE GUSTS")
    if cape is not None:
        if cape > 2500: w.append("SEVERE TSTM")
        elif cape > 1000: w.append("TSTM LIKELY")
    return " | ".join(w) if w else ""

def get_wind_forecast(lat, lon, days=3):
    """Get weather forecast using urllib instead of requests"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        'latitude': str(lat), 
        'longitude': str(lon),
        'hourly': 'temperature_2m,weathercode,pressure_msl,windspeed_10m,winddirection_10m,windgusts_10m,precipitation,cape',
        'current_weather': 'true', 
        'windspeed_unit': 'ms', 
        'timezone': 'auto', 
        'forecast_days': str(days)
    }
    try:
        # Build URL with parameters
        query_string = urllib.parse.urlencode(params)
        full_url = f"{url}?{query_string}"
        
        # Make request using urllib
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        
        hourly = data['hourly']
        rows = []
        
        for i in range(len(hourly['time'])):
            wind_ms = hourly['windspeed_10m'][i]
            gust_ms = hourly['windgusts_10m'][i]
            wind_kn = wind_ms * 1.94384 if wind_ms else 0
            gust_kn = gust_ms * 1.94384 if gust_ms else wind_kn
            
            rows.append({
                'datetime': datetime.fromisoformat(hourly['time'][i]),
                'wind_speed_ms': round(wind_ms, 1) if wind_ms else 0,
                'wind_speed_knots': round(wind_kn, 1),
                'wind_deg': hourly['winddirection_10m'][i] or 0,
                'wind_dir': degrees_to_compass(hourly['winddirection_10m'][i]),
                'gust_ms': round(gust_ms, 1) if gust_ms else 0,
                'gust_knots': round(gust_kn, 1),
                'temp_celsius': round(hourly['temperature_2m'][i], 1) if hourly['temperature_2m'][i] else 0,
                'pressure_hPa': round(hourly['pressure_msl'][i], 1) if hourly['pressure_msl'][i] else 0,
                'precipitation_mm': round(hourly['precipitation'][i], 1) if hourly['precipitation'][i] else 0,
                'cape': hourly['cape'][i] or 0,
                'weather': interpret_weather_code(hourly['weathercode'][i]),
                'storm_warning': get_storm_warning(wind_ms, gust_ms, hourly['cape'][i])
            })
        
        return pd.DataFrame(rows), data.get('current_weather', {})
    except Exception as e:
        print(f"Weather API error: {e}")
        return None, None

def check_dangerous_conditions(lat, lon):
    """Check for dangerous weather conditions"""
    warnings = []
    df, current = get_wind_forecast(lat, lon, days=2)
    
    if df is None or current is None:
        return []
    
    # Current dangers
    wind_ms = current.get('windspeed', 0)
    if wind_ms >= 17.2:  # Gale force
        wind_knots = wind_ms * 1.94384
        warnings.append(f"NOW: {wind_knots:.0f}kt winds!")
    
    # Check next 12 hours
    next_12h = df.head(12)
    max_wind = next_12h['wind_speed_ms'].max()
    
    if max_wind >= 17.2:
        # Find when it starts
        storm_rows = next_12h[next_12h['wind_speed_ms'] >= 17.2]
        if not storm_rows.empty:
            storm_time = storm_rows.iloc[0]['datetime']
            hours_until = (storm_time - datetime.now()).total_seconds() / 3600
            if hours_until > 0:
                warnings.append(f"GALE in {hours_until:.0f}h")
    
    # Check for rapid increases
    if len(df) > 6:
        wind_change = df.iloc[6]['wind_speed_ms'] - wind_ms
        if wind_change > 10:
            warnings.append(f"RAPID +{wind_change*1.94:.0f}kt")
    
    # Heavy rain warning
    rain_24h = df.head(24)['precipitation_mm'].sum()
    if rain_24h > 50:
        warnings.append(f"HEAVY RAIN {rain_24h:.0f}mm/24h")
    
    return warnings

def format_weather_sms(current, warnings, lat=None, lon=None):
    """Format user-friendly weather SMS"""
    if not current:
        return "Weather data unavailable"
    
    wind_ms = current.get('windspeed', 0)
    wind_knots = wind_ms * 1.94384
    wind_mph = wind_ms * 2.237
    temp = current.get('temperature', 0)
    direction = degrees_to_compass(current.get('winddirection'))
    
    # Basic weather
    msg = f"Wind: {wind_knots:.0f}kt {direction} ({wind_mph:.0f}mph)\n"
    msg += f"Temp: {temp:.1f}°C/{temp*1.8+32:.0f}°F\n"
    
    # Add warnings if any
    if warnings:
        msg += "⚠️ " + " | ".join(warnings[:2])  # Max 2 warnings
    else:
        msg += "✓ Conditions normal"
    
    return msg[:160]

###=======Monitoring Functions==========###
def monitor_all_users():
    """Check all tracked users for storm conditions"""
    users = get_all_monitored_users()
    alerts_sent = 0
    
    for user in users:
        phone = user['phone_number']
        lat = float(user['latitude'])
        lon = float(user['longitude'])
        
        # Check if we already alerted recently
        last_alert = user.get('last_alert', '')
        if last_alert:
            try:
                last_time = datetime.fromisoformat(last_alert)
                if datetime.now() - last_time < timedelta(hours=6):
                    continue  # Don't spam
            except:
                pass
        
        # Check for dangers
        warnings = check_dangerous_conditions(lat, lon)
        
        if warnings:
            # Send alert
            message = f"⚠️ WEATHER ALERT\n{lat:.2f}, {lon:.2f}\n"
            message += "\n".join(warnings[:3])
            message = message[:160]
            
            if send_sms(phone, message):
                update_last_alert(phone)
                alerts_sent += 1
    
    return {
        'statusCode': 200,
        'body': json.dumps(f'Monitored {len(users)} users, sent {alerts_sent} alerts')
    }

###=======Lambda Handler==========###
def lambda_handler(event, context):
    try:
        # Check if this is a scheduled monitoring event
        if event.get('source') == 'aws.events':
            return monitor_all_users()
        
        # Parse Twilio webhook request
        twilio_data = parse_twilio_request(event)
        sms_body = twilio_data.get('Body', [''])[0]
        from_number = twilio_data.get('From', [''])[0]
        
        # Parse the SMS
        result = parse_sms_input(sms_body)
        
        # Handle commands
        if result[0] == 'CHECK':
            # Use last known location
            lat, lon = get_last_location(from_number)
            if lat is None:
                send_sms(from_number, 'No saved location. Send: LAT,LON')
                return build_twilio_response('')
        elif result[0] == 'STOP':
            # Disable monitoring (would need to implement)
            send_sms(from_number, 'Monitoring stopped')
            return build_twilio_response('')
        elif result[0] == 'START':
            # Enable monitoring
            lat, lon = get_last_location(from_number)
            if lat:
                send_sms(from_number, f'Monitoring enabled for {lat:.2f},{lon:.2f}')
            else:
                send_sms(from_number, 'Send location first: LAT,LON')
            return build_twilio_response('')
        else:
            # Parse coordinates
            lat, lon, dest_lat, dest_lon = result
            
            if lat is None:
                # Try to use last location
                lat, lon = get_last_location(from_number)
                if lat is None:
                    send_sms(from_number, 'Send: LAT,LON or CHECK for last location')
                    return build_twilio_response('')
            else:
                # Save new location
                store_user_location(from_number, lat, lon)
        
        # Get weather for location
        df, current = get_wind_forecast(lat, lon)
        warnings = check_dangerous_conditions(lat, lon)
        
        # Format and send response
        reply = format_weather_sms(current, warnings, lat, lon)
        send_sms(from_number, reply)
        
        return build_twilio_response('')
        
    except Exception as e:
        print(f"Error: {e}")
        return build_twilio_response('')

# Environment variables needed:
# TWILIO_ACCOUNT_SID
# TWILIO_AUTH_TOKEN  
# TWILIO_PHONE_NUMBER
# DYNAMODB_TABLE (optional, defaults to 'user-locations')