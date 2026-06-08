import urllib.request
import urllib.parse
import json
import pandas as pd
from datetime import datetime
import math
from math import radians, sin, cos, asin, atan2, sqrt, ceil
from urllib.parse import parse_qs
import base64
import os
from twilio.rest import Client

# -------- global forecast horizon --------
FORECAST_HOURS = 50
FORECAST_DAYS = max(3, ceil(FORECAST_HOURS/24) + 1)

###=======Twilio==========###
# Twilio client initialization
def get_twilio_client():
    account_sid = os.environ['TWILIO_ACCOUNT_SID']
    auth_token = os.environ['TWILIO_AUTH_TOKEN']
    return Client(account_sid, auth_token)

# Twilio webhook data parsing
def parse_twilio_request(event):
    if event.get('isBase64Encoded'):
        body = base64.b64decode(event['body']).decode('utf-8')
    else:
        body = event['body']
    return parse_qs(body)

# Send SMS via Twilio API
def send_sms(to_number, message):
    client = get_twilio_client()
    from_number = os.environ['TWILIO_PHONE_NUMBER']
    response = client.messages.create(
        to=to_number,
        from_=from_number,
        body=message[:1600]  # Twilio max length
    )
    return response.sid

# Twilio TwiML response builder (for immediate reply)
def build_twilio_response(message, status_code=200):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/xml',
            'Access-Control-Allow-Origin': '*'
        },
        'body': f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{message}</Message></Response>'
    }
###=======Twilio==========###

# ---------------- Input Parser ----------------
def parse_sms_input(sms_body):
    """
    Parse SMS input format: "59.444, 24.768 -> 59.329, 18.069"
    Returns: (cur_lat, cur_lon, dest_lat, dest_lon)
    """
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
            return None, None, None, None
    except:
        return None, None, None, None

# ---------------- DMS utilities ----------------
def decimal_to_dms(value, is_lat=True):
    hemi = ('N','S') if is_lat else ('E','W')
    sign = hemi[0] if value >= 0 else hemi[1]
    v = abs(float(value))
    deg = int(v)
    m_float = (v - deg) * 60
    minute = int(m_float)
    sec = round((m_float - minute) * 60)
    if sec == 60:
        sec = 0; minute += 1
    if minute == 60:
        minute = 0; deg += 1
    deg_fmt = f"{deg:02d}" if is_lat else f"{deg:03d}"
    return f"{deg_fmt}°{minute:02d}'{sec:02d}\"{sign}"

def dms_to_decimal(deg, minute, sec, hemisphere):
    val = float(deg) + float(minute)/60.0 + float(sec)/3600.0
    if hemisphere.upper() in ('S', 'W'):
        val = -val
    return val

# ---------------- Formatting helpers ----------------
def degrees_to_compass(deg):
    if deg is None: return 'N'
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
            'S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[int((deg + 11.25) / 22.5) % 16]

def interpret_weather_code(code):
    if code is None: return "Unknown"
    m = {0:"Clear sky",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",
         45:"Fog",48:"Depositing rime fog",51:"Light drizzle",53:"Moderate drizzle",
         55:"Dense drizzle",61:"Slight rain",63:"Moderate rain",65:"Heavy rain",
         71:"Slight snow",73:"Moderate snow",75:"Heavy snow",77:"Snow grains",
         80:"Slight rain showers",81:"Moderate rain showers",82:"Violent rain showers",
         85:"Slight snow showers",86:"Heavy snow showers",95:"Thunderstorm",
         96:"TS w/ slight hail",99:"TS w/ heavy hail"}
    return m.get(code, f"Weather code {code}")

def get_storm_warning(wind_ms, gust_ms, cape):
    w = []
    if wind_ms is not None:
        if wind_ms >= 32.7: w.append("HURRICANE FORCE")
        elif wind_ms >= 28.5: w.append("VIOLENT STORM")
        elif wind_ms >= 24.5: w.append("STORM")
        elif wind_ms >= 20.8: w.append("STRONG GALE")
        elif wind_ms >= 17.2: w.append("GALE")
    if gust_ms and wind_ms and gust_ms - wind_ms > 10: w.append("SEVERE GUSTS")
    if cape is not None:
        if cape > 2500: w.append("SEVERE TSTM RISK")
        elif cape > 1000: w.append("TSTM POSSIBLE")
    return " | ".join(w) if w else ""

# ---------------- Core API helpers ----------------
def get_wind_forecast(lat, lon, days=FORECAST_DAYS):
    """Get weather forecast using urllib instead of requests"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        'latitude': str(lat),
        'longitude': str(lon),
        'hourly': 'temperature_2m,weathercode,pressure_msl,windspeed_10m,windspeed_80m,windspeed_120m,winddirection_10m,windgusts_10m,precipitation,visibility,cape',
        'daily': 'weathercode,windspeed_10m_max,windgusts_10m_max,winddirection_10m_dominant',
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
            wind_kn = wind_ms * 1.94384 if wind_ms is not None else 0
            gust_kn = gust_ms * 1.94384 if gust_ms is not None else wind_kn
            wind_deg = hourly['winddirection_10m'][i]
            cape = hourly['cape'][i] if hourly['cape'][i] else 0
            dt = datetime.fromisoformat(hourly['time'][i].replace('Z',''))
            rows.append({
                'datetime': dt,
                'wind_speed_ms': round(wind_ms, 1) if wind_ms is not None else 0,
                'wind_speed_knots': round(wind_kn, 1),
                'wind_deg': wind_deg if wind_deg is not None else 0,
                'wind_dir': degrees_to_compass(wind_deg) if wind_deg is not None else 'N',
                'gust_ms': round(gust_ms, 1) if gust_ms is not None else 0,
                'gust_knots': round(gust_kn, 1),
                'temp_celsius': round(hourly['temperature_2m'][i], 1) if hourly['temperature_2m'][i] is not None else 0,
                'pressure_hPa': round(hourly['pressure_msl'][i], 1) if hourly['pressure_msl'][i] is not None else 0,
                'precipitation_mm': round(hourly['precipitation'][i], 1) if hourly['precipitation'][i] is not None else 0,
                'visibility_m': hourly['visibility'][i] if hourly['visibility'][i] is not None else 0,
                'cape': cape,
                'weather': interpret_weather_code(hourly['weathercode'][i]),
                'storm_warning': get_storm_warning(wind_ms, gust_ms, cape)
            })
        return pd.DataFrame(rows), data.get('current_weather', {})
    except Exception as e:
        print(f"API error: {e}")
        return None, None

def get_current_only(lat, lon):
    """Get current weather only using urllib"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        'latitude': str(lat),
        'longitude': str(lon),
        'current_weather': 'true',
        'timezone': 'auto',
        'windspeed_unit': 'ms'
    }
    try:
        query_string = urllib.parse.urlencode(params)
        full_url = f"{url}?{query_string}"
        
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        
        return data.get('current_weather', {})
    except Exception as e:
        print(f"API error: {e}")
        return {}

# ---------------- SMS builders ----------------
def _fmt_hour_chunk(row, style=1):
    hh = row['datetime'].strftime('%Hh')
    ws = int(round(row['wind_speed_knots']))
    gs = int(round(row['gust_knots']))
    deg = int(round(row['wind_deg']))
    if style == 1:
        return f"{hh} {ws}kt {deg}° {gs}kt"
    else:
        return f"{hh} {ws} {deg} {gs}"

def build_sms_next_hours(df, start_idx=0, hours=8, max_len=160):
    rows = [df.iloc[i] for i in range(start_idx, min(start_idx+hours, len(df)))]
    variants = [
        lambda r: _fmt_hour_chunk(r, style=1),
        lambda r: _fmt_hour_chunk(r, style=2),
    ]
    for v in variants:
        s = '; '.join(v(r) for r in rows)
        if len(s) <= max_len:
            return s
    for n in range(hours-1, 0, -1):
        s = '; '.join(variants[-1](r) for r in rows[:n])
        if len(s) <= max_len:
            return s
    return variants[-1](rows[0])[:max_len]

def _signed_dir_delta_deg(a, b):
    if a is None or b is None: return 0
    return (b - a + 540) % 360 - 180

def build_sms_next_hours_enhanced(df, start_idx=0, hours=8, max_len=160, lat=None, lon=None, ring_deg=1.0):
    if df is None or df.empty: return ""
    base = build_sms_next_hours(df, start_idx=start_idx, hours=hours, max_len=max_len)
    i0 = start_idx
    i1 = min(start_idx+hours-1, len(df)-1)
    ws0 = float(df.iloc[i0]['wind_speed_knots'])
    ws1 = float(df.iloc[i1]['wind_speed_knots'])
    wd0 = float(df.iloc[i0]['wind_deg'])
    wd1 = float(df.iloc[i1]['wind_deg'])
    trend = ws1 - ws0
    arrow = '↑' if trend >= 2 else ('↓' if trend <= -2 else '')
    dshift = int(round(_signed_dir_delta_deg(wd0, wd1)))
    shift_tag = ''
    if abs(dshift) >= 15:
        shift_tag = f" V{dshift}°" if dshift > 0 else f" B{abs(dshift)}°"
    extras = (f" {arrow}" if arrow else '') + (shift_tag if shift_tag else '')
    if len(base) + len(extras) <= max_len:
        return base + extras
    return base

def _rain_class(mm):
    if mm is None: return None
    if mm >= 10: return 'RS'
    if mm >= 2:  return 'RM'
    if mm >= 0.1:return 'RL'
    return None

def build_rain_tokens(df, start_idx=0, hours=8):
    if df is None or df.empty: return ''
    cls_hours = {'RS':[], 'RM':[], 'RL':[]}
    for off in range(1, hours+1):
        i = start_idx + off - 1
        if i >= len(df): break
        mm = df.iloc[i].get('precipitation_mm', 0) or 0
        c = _rain_class(mm)
        if c:
            cls_hours[c].append(str(off))
    parts = []
    for tag in ['RS','RM','RL']:
        if cls_hours[tag]:
            parts.append(tag + ''.join(cls_hours[tag]))
    return ' '.join(parts)

def build_sms_with_rain(df, start_idx=0, hours=8, max_len=160, lat=None, lon=None, ring_deg=1.0):
    base = build_sms_next_hours_enhanced(df, start_idx, hours, max_len, lat, lon, ring_deg)
    rain = build_rain_tokens(df, start_idx, hours)
    if not rain: return base
    return base if len(base)+1+len(rain) > max_len else base + ' ' + rain

def detect_alerts(df, hours=8):
    sub = df.head(hours)
    max_ws = sub['wind_speed_knots'].max()
    max_gs = sub['gust_knots'].max()
    tags = []
    if max_ws >= 34: tags.append('GALE')
    if max_ws >= 41: tags.append('STRONG GALE')
    if max_ws >= 48: tags.append('STORM')
    if max_gs >= 50: tags.append('SEVERE GUSTS')
    return ' | '.join(sorted(set(tags), key=tags.index))

# ---------------- Area ring snapshot ----------------
def wrap_lon(lon):
    if lon > 180: lon -= 360
    if lon < -180: lon += 360
    return lon

def area_points(lat, lon, radius_deg):
    pts = [
        ("N",  lat + radius_deg, lon),
        ("S",  lat - radius_deg, lon),
        ("E",  lat,              wrap_lon(lon + radius_deg)),
        ("W",  lat,              wrap_lon(lon - radius_deg)),
        ("NE", lat + radius_deg, wrap_lon(lon + radius_deg)),
        ("NW", lat + radius_deg, wrap_lon(lon - radius_deg)),
        ("SE", lat - radius_deg, wrap_lon(lon + radius_deg)),
        ("SW", lat - radius_deg, wrap_lon(lon - radius_deg)),
    ]
    pts = [(d, max(min(la, 90), -90), lo) for d, la, lo in pts]
    return pts

def ring_snapshot(lat, lon, radius_deg):
    out = []
    for d, la, lo in area_points(lat, lon, radius_deg):
        try:
            cur = get_current_only(la, lo)
            if not cur: continue
            ws = cur.get('windspeed', 0)
            gs = cur.get('windgusts', ws)
            wd = int(round(cur.get('winddirection', 0)))
            out.append((d, int(round(ws*1.94384)), wd, int(round(gs*1.94384))))
        except Exception:
            continue
    return out

def build_sms_ring_summary(lat, lon, radius_deg=1.0, max_len=160):
    items = ring_snapshot(lat, lon, radius_deg)
    parts = [f"{d}{ws}kt{wd}°/{gs}kt" for d, ws, wd, gs in items]
    s = ' '.join(parts)
    return s[:max_len]

# ---------------- Routing math ----------------
def initial_bearing_deg(lat1, lon1, lat2, lon2):
    lat1r, lat2r, dlon = radians(lat1), radians(lat2), radians(lon2-lon1)
    x = sin(dlon) * cos(lat2r)
    y = cos(lat1r)*sin(lat2r) - sin(lat1r)*cos(lat2r)*cos(dlon)
    brng = atan2(x, y) * 180/math.pi
    return (brng + 360) % 360

def haversine_nm(lat1, lon1, lat2, lon2):
    R_nm = 3440.065
    dlat = radians(lat2-lat1); dlon = radians(lon2-lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2*asin(sqrt(a))
    return R_nm*c

def gc_interp(lat1, lon1, lat2, lon2, f):
    if f <= 0: return lat1, lon1
    if f >= 1: return lat2, lon2
    lat1r, lon1r, lat2r, lon2r = map(radians, [lat1, lon1, lat2, lon2])
    d = 2*asin(sqrt(sin((lat2r-lat1r)/2)**2 + cos(lat1r)*cos(lat2r)*sin((lon2r-lon1r)/2)**2))
    if d == 0: return lat1, lon1
    A = sin((1-f)*d)/sin(d); B = sin(f*d)/sin(d)
    x = A*cos(lat1r)*cos(lon1r) + B*cos(lat2r)*cos(lon2r)
    y = A*cos(lat1r)*sin(lon1r) + B*cos(lat2r)*sin(lon2r)
    z = A*sin(lat1r) + B*sin(lat2r)
    lat = atan2(z, sqrt(x*x + y*y)); lon = atan2(y, x)
    return (lat*180/math.pi, ((lon*180/math.pi + 540)%360)-180)

# ---------------- Vessel speed model ----------------
def calc_hull_speed_knots(loa_m, lwl_factor=0.9):
    lwl_m = max(1.0, float(loa_m) * lwl_factor)
    lwl_ft = lwl_m * 3.28084
    return 1.34 * (lwl_ft ** 0.5)

def polar_efficiency(twa_deg):
    a = abs((twa_deg % 360.0))
    theta = a if a <= 180.0 else 360.0 - a
    phi = math.radians(theta)
    g = (sin(phi) ** 1.1) * (cos(phi / 2.0) ** 1.7)
    return max(0.0, min(0.55, float(g)))

def estimate_speed_knots(loa_m, beam_m, tws_kn, twa_deg):
    H = calc_hull_speed_knots(loa_m)
    T = max(0.0, float(tws_kn))
    if T <= 18:
        F = 1.00
    elif T <= 30:
        F = 1.00 - 0.015 * (T - 18)
    elif T <= 40:
        F = 0.82 - 0.010 * (T - 30)
    else:
        F = 0.72
    v = F * polar_efficiency(twa_deg) * T
    return max(0.0, min(H, v))

# ---------------- Route builders ----------------
def route_hourly_forecast_speed_model(cur_lat, cur_lon, dest_lat, dest_lon, loa_m, beam_m, hours=FORECAST_HOURS):
    rows = []
    lat, lon = float(cur_lat), float(cur_lon)
    for t in range(hours):
        df, _ = get_wind_forecast(lat, lon, days=FORECAST_DAYS)
        if df is None or df.empty: break
        r = df.iloc[min(t, len(df)-1)].copy()
        brg = initial_bearing_deg(lat, lon, dest_lat, dest_lon)
        twd = float(r['wind_deg'])
        twa = (twd - brg + 360) % 360; twa = twa if twa <= 180 else 360 - twa
        tws = float(r['wind_speed_knots'])
        sog = estimate_speed_knots(loa_m, beam_m, tws, twa)
        rem = max(1e-6, haversine_nm(lat, lon, dest_lat, dest_lon))
        step_nm = min(rem, sog * 1.0)
        frac = step_nm / rem
        nlat, nlon = gc_interp(lat, lon, dest_lat, dest_lon, frac)
        r['route_lat'], r['route_lon'] = nlat, nlon
        r['course_deg'] = brg
        r['twa_deg'] = twa
        r['sog_est_kn'] = sog
        rows.append(r)
        lat, lon = nlat, nlon
        if step_nm >= rem - 1e-3: break
    return pd.DataFrame(rows)

def build_route_sms(rdf, hours=8, max_len=160, lat=None, lon=None):
    if rdf is None or rdf.empty: return ""
    return build_sms_with_rain(rdf, start_idx=0, hours=min(hours, len(rdf)), max_len=max_len, lat=lat, lon=lon, ring_deg=1.0)

# ---------------- Hazards ----------------
def _angdiff(a, b): return ((b - a + 540) % 360) - 180

def analyze_route_hazards(rdf, lookahead_h=8):
    if rdf is None or rdf.empty: return ""
    sub = rdf.head(lookahead_h).reset_index(drop=True)
    ws_max = float(sub['wind_speed_knots'].max()); gs_max = float(sub['gust_knots'].max())
    tier = ""
    if ws_max >= 63: tier = "HURR"
    elif ws_max >= 56: tier = "VSTORM"
    elif ws_max >= 48: tier = "STORM"
    elif ws_max >= 41: tier = "SGALE"
    elif ws_max >= 34: tier = "GALE"
    severe_gusts = gs_max >= 50
    shift_tag = ""
    for k in range(1, min(3, len(sub)-1) + 1):
        d = int(round(_angdiff(float(sub.loc[0,'wind_deg']), float(sub.loc[k,'wind_deg']))))
        if abs(d) >= 30:
            shift_tag = f"{'V' if d>0 else 'B'}{abs(d)}°@{sub.loc[k,'datetime'].strftime('%Hh')}"
            break
    ts_risk = any(("Thunder" in str(x)) for x in sub['weather'])
    heavy_rain = any((p if p is not None else 0) >= 10 for p in sub['precipitation_mm'])
    low_vis = any((v if v is not None else 9999) < 1000 for v in sub['visibility_m'])
    tokens = []
    if tier: tokens.append(tier)
    if severe_gusts: tokens.append("SEV_GUSTS")
    if shift_tag: tokens.append(f"SHIFT {shift_tag}")
    if ts_risk: tokens.append("TS_RISK")
    if heavy_rain: tokens.append("HEAVY_RAIN")
    if low_vis: tokens.append("LOW_VIS")
    return "OK" if not tokens else ("WARN " + " | ".join(tokens))[:160]

###=======Twilio==========###
# Lambda handler for Twilio webhook and outbound SMS
def lambda_handler(event, context):
    try:
        # Parse Twilio webhook request
        twilio_data = parse_twilio_request(event)
        sms_body = twilio_data.get('Body', [''])[0]
        from_number = twilio_data.get('From', [''])[0]
        
        # Parse coordinates from SMS
        cur_lat, cur_lon, dest_lat, dest_lon = parse_sms_input(sms_body)
        
        if cur_lat is None or cur_lon is None or dest_lat is None or dest_lon is None:
            # Send error response via Twilio API
            send_sms(from_number, 'Format: LAT,LON->DLAT,DLON')
            return build_twilio_response('')  # Empty TwiML response
        
        # Default vessel parameters
        loa_m = 23.0
        beam_m = 5.0
        
        # Calculate route with speed model
        rdf = route_hourly_forecast_speed_model(cur_lat, cur_lon, dest_lat, dest_lon, loa_m, beam_m, hours=FORECAST_HOURS)
        
        if rdf.empty:
            send_sms(from_number, 'Route calculation failed')
            return build_twilio_response('')
        
        # Build SMS response
        route_sms = build_route_sms(rdf, hours=8, max_len=160, lat=cur_lat, lon=cur_lon)
        
        # Add ETA if available
        eta_hours = len(rdf)
        if eta_hours > 0:
            p1 = rdf.iloc[0]
            route_sms = f"SOG~{p1['sog_est_kn']:.1f}kt ETA~{eta_hours}h\n{route_sms}"
        
        # Check for hazards along route
        hazards = analyze_route_hazards(rdf, lookahead_h=8)
        if hazards != "OK":
            route_sms = f"{hazards}\n{route_sms}"
        
        # Send SMS via Twilio API
        send_sms(from_number, route_sms[:160])
        
        # Return empty TwiML response (SMS already sent via API)
        return build_twilio_response('')
        
    except Exception as e:
        print(f"Error: {e}")
        return build_twilio_response('')
###=======Twilio==========###

# Required environment variables:
# TWILIO_ACCOUNT_SID
# TWILIO_AUTH_TOKEN
# TWILIO_PHONE_NUMBER