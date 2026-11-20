import polars as pl


def run(input, options, contexts):
    input = input.with_columns(
        pl.when(pl.col("temperature") <= 0)
        .then(pl.lit("저온 지역"))
        .when(pl.col("temperature") >= 37)
        .then(pl.lit("고온 지역"))
        .otherwise(pl.lit("보통 지역"))
        .alias("temperature_zone")
    )
    return {"output": input}


if __name__ == "__main__":
    df = pl.DataFrame(
        {
            "latitude": [37.45, 37.5, 37.6, 37.7],
            "longitude": [126.4375, 127, 127.5, 128],
            "temperature": [0.3, -1.2, 37.5, 20.0],
            "windspeed": [27.4, 12, 15.3, 8.2],
            "winddirection": [332, 309, 270, 180],
        }
    )
    result = run(input=df, options={}, contexts={})
    print(result["output"])
