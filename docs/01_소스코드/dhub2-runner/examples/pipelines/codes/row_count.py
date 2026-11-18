import polars as pl


def run(input, options, contexts):
    # Publish a scalar to the `row_count` variable channel (see the manifest
    # output binding). The return dict key "count" is the step-local name;
    # the channel name lives in the output PipelineData.variable.
    return {"count": input.height}


if __name__ == "__main__":
    df = pl.DataFrame(
        {
            "name": ["Alice", "Bob", "Charlie"],
            "price": [2500, 3200, 4000],
        }
    )
    out = run(input=df, options={}, contexts={})
    print(out)
