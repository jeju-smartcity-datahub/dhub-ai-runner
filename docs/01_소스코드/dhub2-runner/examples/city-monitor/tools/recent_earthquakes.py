from datetime import datetime, timedelta, timezone


@use_connector("usgs", ref="usgs_earthquake")
def execute(hours=24, min_magnitude=4.5):
    since = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    payload = usgs.get(
        "query",
        {
            "format": "geojson",
            "starttime": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmagnitude": min_magnitude,
            "orderby": "magnitude",
            "limit": 10,
        },
    )
    return {
        "earthquakes": [
            {
                "magnitude": f["properties"]["mag"],
                "place": f["properties"]["place"],
                "time": datetime.fromtimestamp(f["properties"]["time"] / 1000, timezone.utc).isoformat(),
                "depth_km": f["geometry"]["coordinates"][2],
            }
            for f in payload.get("features", [])
        ]
    }
