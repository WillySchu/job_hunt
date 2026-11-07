"""A stand-in for the Claude Messages API, shaped like its replies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

KEY = "sk-ant-test"


@dataclass
class FakeClaude:
    """Scores by title: anything with "Backend" 82, "Data" 64, else 35. `fail` holds statuses to answer
    first, in order (e.g. [529] fails one call, then works)."""
    fail: list[int] = field(default_factory=list)
    stop_reason: str = "end_turn"
    text: str | None = None          # replace the JSON text block (to test bad replies)
    calls: list[dict] = field(default_factory=list)
    headers: list[dict] = field(default_factory=list)

    def score_for(self, posting: str) -> int:
        title = re.search(r"Title: (.*)", posting).group(1)
        return 82 if "Backend" in title else 64 if "Data" in title else 35

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        self.headers.append(dict(request.headers))
        if request.headers.get("x-api-key") != KEY:
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}})
        if self.fail:
            status = self.fail.pop(0)
            return httpx.Response(status, json={"type": "error", "error": {"type": "overloaded_error"}},
                                  headers={"retry-after": "1"})
        body = json.loads(request.content)
        self.calls.append(body)
        first = len(self.calls) == 1
        posting = body["messages"][0]["content"]
        score = self.score_for(posting)
        text = self.text if self.text is not None else json.dumps(
            {"score": score, "reason": f"Strong Go and Kubernetes match; scored {score}."})
        return httpx.Response(200, json={
            "id": f"msg_{len(self.calls)}", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "thinking", "thinking": "Weighing the core stack.", "signature": "x"},
                        {"type": "text", "text": text}],
            "stop_reason": self.stop_reason,
            "usage": {"input_tokens": 1200, "output_tokens": 250,
                      "cache_creation_input_tokens": 1400 if first else 0,
                      "cache_read_input_tokens": 0 if first else 1400},
        })
