CITIES = {
    "seoul": (37.57, 126.98),
    "busan": (35.18, 129.08),
    "daejeon": (36.35, 127.38),
    "gwangju": (35.16, 126.85),
    "jeju": (33.50, 126.53),
}


@use_connector("weather_api", ref="open_meteo")
@use_connector("air_api", ref="open_meteo_air")
def execute(city):
    latitude, longitude = CITIES[city]
    weather = weather_api.get(
        "forecast",
        {"latitude": latitude, "longitude": longitude, "current": "temperature_2m,wind_speed_10m,precipitation"},
    )["current"]
    air = air_api.get(
        "air-quality", {"latitude": latitude, "longitude": longitude, "current": "pm10,pm2_5"}
    )["current"]
    return {
        "city": city,
        "time": weather["time"],
        "temperature": weather["temperature_2m"],
        "wind_speed_kmh": weather["wind_speed_10m"],
        "precipitation_mm": weather["precipitation"],
        "pm10": air["pm10"],
        "pm2_5": air["pm2_5"],
    }
