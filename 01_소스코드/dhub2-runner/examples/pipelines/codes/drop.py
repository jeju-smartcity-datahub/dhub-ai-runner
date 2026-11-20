import polars as pl


def run(input, options, contexts):
    input = input.drop("price")
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
