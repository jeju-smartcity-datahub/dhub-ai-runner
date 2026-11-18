import polars as pl


def run(city_daily, earthquakes, options=None, contexts=None):
    """What the analyst agent reads: one entry per city and day, and the quakes."""
    days = city_daily.sort("date", "city").with_columns(pl.col("date").cast(pl.String))
    quakes = (
        earthquakes.sort("magnitude", descending=True)
        .head(10)
        .with_columns(pl.col("time").cast(pl.String))
        if earthquakes.height
        else earthquakes
    )
    return {
        "brief": {
            "city_days": days.to_dicts(),
            "earthquakes": quakes.to_dicts(),
            "thresholds": {"pm10_bad": 80, "pm2_5_bad": 35, "heavy_rain_mm": 30, "strong_wind_kmh": 50},
        }
    }
