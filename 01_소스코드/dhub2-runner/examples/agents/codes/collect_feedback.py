def run(feedback, options=None, contexts=None):
    return {"items": feedback.select("channel", "text").to_dicts()}
