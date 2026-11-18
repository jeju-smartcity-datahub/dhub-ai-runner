from datetime import datetime

import polars as pl


@use_connector("air_api", ref="open_meteo_air")
def run(cities, options=None, contexts=None):
    days = int((options or {}).get("forecast_days", 2))
    rows = []
    for city in cities.iter_rows(named=True):
        payload = air_api.get(
            "air-quality",
            {
                "latitude": city["latitude"],
                "longitude": city["longitude"],
                "hourly": "pm10,pm2_5",
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
                    "pm10": hourly["pm10"][i],
                    "pm2_5": hourly["pm2_5"][i],
                }
            )
    return {
        "air": pl.DataFrame(
            rows,
            schema={"city": pl.String, "time": pl.Datetime("us"), "pm10": pl.Float64, "pm2_5": pl.Float64},
        )
    }
