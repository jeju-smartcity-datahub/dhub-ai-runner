import json
import re

INSTRUCTIONS = """당신은 한국 주요 도시의 환경 위험을 분석하는 분석가입니다.
- 입력은 JSON 요약(city_days, earthquakes, thresholds)이며, 그 데이터만 근거로 분석합니다.
- thresholds 를 넘는 미세먼지(PM10, PM2.5), 강수, 강풍이 있는 도시와 날짜를 경보로 정리합니다.
- 한국 주변(위도 30~40, 경도 120~135) 지진이나 규모 6.0 이상 지진은 경보에 넣습니다.
- 답은 아래 형식의 JSON 객체 하나로만 씁니다. 설명이나 코드 블록 없이 JSON 만 씁니다.
  {"risk_level": "low|moderate|high", "summary": "전체 상황 두세 문장",
   "alerts": [{"city": "...", "date": "YYYY-MM-DD", "kind": "pm10|pm2_5|rain|wind|earthquake", "detail": "..."}]}
- summary 와 detail 은 한국어로, 수치와 함께 짧게 씁니다."""


@use_llm("llm", model="gemma4")
def run(brief, options=None, contexts=None):
    reply = llm.invoke(
        [
            ("system", INSTRUCTIONS),
            ("human", json.dumps(brief, ensure_ascii=False, default=str)),
        ]
    ).content
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        raise ValueError(f"the model did not answer with JSON: {reply[:200]}")
    analysis = json.loads(match.group(0))
    if analysis.get("risk_level") not in ("low", "moderate", "high"):
        raise ValueError(f"unexpected risk_level: {analysis.get('risk_level')!r}")
    return {"analysis": analysis}
