import json
import re

INSTRUCTIONS = """고객 피드백 목록(JSON)을 받아 전체 분위기와 반복되는 문제를 한국어로 요약합니다.
아래 형식의 JSON 객체 하나로만 답합니다. 설명이나 코드 블록 없이 JSON 만 씁니다.
{"summary": "두세 문장 요약", "sentiment": "positive|neutral|negative",
 "top_issues": ["가장 많이 나온 문제, 최대 3개"]}"""


@use_llm("llm", model="gemma4")
def run(feedback, options=None, contexts=None):
    reply = llm.invoke(
        [("system", INSTRUCTIONS), ("human", json.dumps(feedback, ensure_ascii=False))]
    ).content
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        raise ValueError(f"the model did not answer with JSON: {reply[:200]}")
    return {"summary": json.loads(match.group(0))}
