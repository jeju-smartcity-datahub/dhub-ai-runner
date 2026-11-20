import polars as pl


def run(input, options, contexts):
    input = input.with_columns((pl.col("name").str.slice(0, 2) + "***").alias("name"))
    return {"output": input}


if __name__ == "__main__":
    df = pl.DataFrame(
        {
            "name": ["Alice", "Bob", "Charlie"],
            "price": [2500, 3200, 4000],
        }
    )
    df = run(input=df, options={}, contexts={})
    print(df)
