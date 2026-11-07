"""
Weather skill — pulls current weather and short forecast for the user's location.

Uses Open-Meteo, which is a free weather API with no key required.
There is no built-in location: set it in config:

  weather:
    lat: 40.71
    lon: -74.01
    location_name: "home"      # optional label
    timezone: "auto"           # optional, IANA name or "auto" (default)
"""

import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime
from skills.base import BaseSkill

logger = logging.getLogger(__name__)


class WeatherSkill(BaseSkill):
    name = "weather"

    def __init__(self, config: dict):
        super().__init__(config)
        weather_cfg = config.get("weather", {}) or {}
        # No default location: lat/lon must come from config.
        self.lat = weather_cfg.get("lat")
        self.lon = weather_cfg.get("lon")
        self.timezone = weather_cfg.get("timezone") or "auto"
        self.user_name = (
            config.get("user_nickname") or config.get("user_name") or "the user"
        )
        self.location_name = weather_cfg.get("location_name") or f"{self.user_name}'s place"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": (
                        f"Get the current weather and short forecast for {self.user_name}'s location "
                        f"(set in config). Use this when {self.user_name} mentions being "
                        "outside or heading out, or you just want to know "
                        "what kind of day it is in their sky. Returns temperature in "
                        "Fahrenheit, conditions, wind, and a 3-day outlook."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "get_weather":
            return self._fetch_weather()
        return f"Unknown tool: {tool_name}"

    def _fetch_weather(self) -> str:
        if self.lat is None or self.lon is None:
            return (
                "No weather location is configured. Set weather.lat and "
                "weather.lon (and optionally weather.location_name) in config."
            )
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": self.timezone,
            "forecast_days": 3,
        }
        url = f"https://api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(params)}"

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"Weather fetch failed: {e}")
            return f"Couldn't reach the weather service: {e}"

        current = data.get("current", {})
        daily = data.get("daily", {})

        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        precip = current.get("precipitation", 0)
        code = current.get("weather_code")
        is_day = current.get("is_day", 1)

        condition = WEATHER_CODES.get(code, "weird sky")

        lines = [f"Weather for {self.location_name}:"]
        lines.append(f"  Right now: {condition}, {temp}°F (feels like {feels}°F)")
        lines.append(f"  Humidity: {humidity}%, Wind: {wind} mph")
        if precip:
            lines.append(f"  Precipitation: {precip} inches")
        lines.append(f"  Time of day: {'daytime' if is_day else 'nighttime'}")

        # Forecast
        days = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])
        precip_probs = daily.get("precipitation_probability_max", [])

        if days:
            lines.append("")
            lines.append("Next 3 days:")
            for i, day in enumerate(days[:3]):
                day_name = "Today" if i == 0 else "Tomorrow" if i == 1 else datetime.strptime(day, "%Y-%m-%d").strftime("%A")
                cond = WEATHER_CODES.get(codes[i], "weird sky") if i < len(codes) else "?"
                hi = max_temps[i] if i < len(max_temps) else "?"
                lo = min_temps[i] if i < len(min_temps) else "?"
                rain = precip_probs[i] if i < len(precip_probs) else 0
                rain_str = f", {rain}% chance of rain" if rain else ""
                lines.append(f"  {day_name}: {cond}, {lo}°-{hi}°F{rain_str}")

        return "\n".join(lines)


# WMO weather codes — https://open-meteo.com/en/docs
WEATHER_CODES = {
    0: "clear sky",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "heavy rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}
