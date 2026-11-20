from datetime import datetime, timedelta, timezone

import polars as pl


@use_connector("usgs", ref="usgs_earthquake")
def run(options=None, contexts=None):
    options = options or {}
    since = datetime.now(timezone.utc) - timedelta(hours=int(options.get("hours", 24)))
    payload = usgs.get(
        "query",
        {
            "format": "geojson",
            "starttime": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmagnitude": options.get("min_magnitude", 4.0),
            "orderby": "magnitude",
            "limit": options.get("limit", 50),
        },
    )
    rows = []
    for feature in payload.get("features", []):
        props = feature["properties"]
        longitude, latitude, depth = feature["geometry"]["coordinates"]
        rows.append(
            {
                "id": feature["id"],
                "time": datetime.fromtimestamp(props["time"] / 1000, timezone.utc).replace(tzinfo=None),
                "magnitude": float(props["mag"]),
                "place": props.get("place"),
                "latitude": float(latitude),
                "longitude": float(longitude),
                "depth_km": float(depth),
                "tsunami": bool(props.get("tsunami")),
            }
        )
    return {
        "earthquakes": pl.DataFrame(
            rows,
            schema={
                "id": pl.String,
                "time": pl.Datetime("us"),
                "magnitude": pl.Float64,
                "place": pl.String,
                "latitude": pl.Float64,
                "longitude": pl.Float64,
                "depth_km": pl.Float64,
                "tsunami": pl.Boolean,
            },
        )
    }
