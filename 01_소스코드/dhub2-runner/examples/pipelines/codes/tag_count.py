import polars as pl


def run(input, n, options, contexts):
    # `n` is the step-local kwarg bound to the `row_count` variable channel
    # (see the manifest input binding). The producing step published it to
    # contexts["row_count"]; dhub2 injects it here as the `n` argument.
    return {"output": input.with_columns(pl.lit(n).alias("total_rows"))}


if __name__ == "__main__":
    df = pl.DataFrame(
        {
            "name": ["Alice", "Bob", "Charlie"],
            "price": [2500, 3200, 4000],
        }
    )
    out = run(input=df, n=3, options={}, contexts={})
    print(out)
