from datetime import datetime, timezone

import polars as pl

POINTS = [
    ("서울", 37.57, 126.98),
    ("강릉", 37.75, 128.88),
    ("부산", 35.18, 129.08),
    ("제주", 33.50, 126.53),
]


@use_connector("weather_api", ref="open_meteo")
def run(options=None, contexts=None):
    collected_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for name, latitude, longitude in POINTS:
        payload = weather_api.get(
            "forecast",
            {"latitude": latitude, "longitude": longitude, "current_weather": "true"},
        )
        current = payload["current_weather"]
        rows.append(
            {
                "location": name,
                "latitude": latitude,
                "longitude": longitude,
                "temperature": float(current["temperature"]),
                "windspeed": float(current["windspeed"]),
                "winddirection": float(current["winddirection"]),
                "collected_at": collected_at,
            }
        )
    return {"output": pl.DataFrame(rows)}
