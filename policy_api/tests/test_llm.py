"""What the judge sends to vLLM."""
import asyncio
import json

import httpx
from openai import AsyncOpenAI

from app.diff import Change
from app.llm import LLMJudge
from app.prompts import Pair
from app.store import ClauseRow

VERDICT = {"impacted": "no", "impact_type": "none", "why": "Unrelated.", "evidence_head": "", "evidence_candidate": "",
           "suggested_edit": ""}


def _judge(sent: list, **kw) -> LLMJudge:
    def reply(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "judge",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": json.dumps(VERDICT)}}]})

    j = LLMJudge("http://judge/v1", "k", "judge", **kw)
    j.client = AsyncOpenAI(base_url="http://judge/v1", api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    return j


PAIR = Pair("POL-007", "Access", Change("4.2", "modified", "", "monthly", "weekly"),
            ClauseRow("SAF-112", "2.2", "", "Checks are monthly."), "Contractors", frozenset({"term"}))


def test_granite_thinking_switch_is_sent():
    sent = []
    asyncio.run(_judge(sent, template_kwargs={"enable_thinking": False}).assess(PAIR))
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent[0]["temperature"] == 0 and sent[0]["response_format"]["type"] == "json_schema"


def test_no_template_switches_for_other_models():
    sent = []
    asyncio.run(_judge(sent).assess(PAIR))
    assert "chat_template_kwargs" not in sent[0]
