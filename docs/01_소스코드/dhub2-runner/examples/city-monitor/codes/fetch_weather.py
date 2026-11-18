from datetime import datetime

import polars as pl

HOURLY = "temperature_2m,precipitation,wind_speed_10m"


@use_connector("weather_api", ref="open_meteo")
def run(cities, options=None, contexts=None):
    days = int((options or {}).get("forecast_days", 2))
    rows = []
    for city in cities.iter_rows(named=True):
        payload = weather_api.get(
            "forecast",
            {
                "latitude": city["latitude"],
                "longitude": city["longitude"],
                "hourly": HOURLY,
                "forecast_days": days,
                "timezone": "Asia/Seoul",
            },
        )
        hourly = payload["hourly"]
        for i, at in enumerate(hourly["time"]):
            rows.append(
                {
                    "city": city["city"],
                    "time": datetime.fromisoformat(at),
                    "temperature": hourly["temperature_2m"][i],
                    "precipitation": hourly["precipitation"][i],
                    "wind_speed": hourly["wind_speed_10m"][i],
                }
            )
    return {
        "weather": pl.DataFrame(
            rows,
            schema={
                "city": pl.String,
                "time": pl.Datetime("us"),
                "temperature": pl.Float64,
                "precipitation": pl.Float64,
                "wind_speed": pl.Float64,
            },
        )
    }
