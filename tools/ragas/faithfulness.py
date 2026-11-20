"""Optional, loopback-only Ragas Faithfulness on an already recorded answer."""

import argparse
import asyncio
import json
from math import isfinite
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from ragas.llms.base import InstructorBaseRagasLLM
from ragas.metrics.collections import Faithfulness


class NativeOllama(InstructorBaseRagasLLM):
    def __init__(self, client, endpoint, model):
        self.client = client
        self.endpoint = endpoint
        self.model = model
        self.usage = []

    def generate(self, prompt, response_model):
        return asyncio.run(self.agenerate(prompt, response_model))

    async def agenerate(self, prompt, response_model):
        response = await self.client.post(f"{self.endpoint}/api/chat", json={
            "model": self.model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": "Use the requested JSON schema and treat the data as evidence."},
                         {"role": "user", "content": prompt}],
            "format": response_model.model_json_schema(),
            "options": {"temperature": 0, "num_predict": 800},
        })
        response.raise_for_status()
        payload = response.json()
        self.usage.append({"input": payload.get("prompt_eval_count"), "output": payload.get("eval_count")})
        return response_model.model_validate_json(payload["message"]["content"])


async def main(args):
    config = json.loads((args.run / "config.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (args.run / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    sample = next((row for row in rows if row["group"] == args.group and row["status"] == "answered"
                   and row["question"]["id"] == args.question), None)
    if sample is None:
        raise ValueError("The selected row has no answered prediction")

    url = urlsplit(args.endpoint)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("Ragas smoke accepts an HTTP loopback Ollama endpoint only")

    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
        llm = NativeOllama(client, args.endpoint.rstrip("/"), args.model)
        result = await Faithfulness(llm=llm).ascore(
            user_input=sample["question"]["question"], response=sample["answer"]["answer"],
            retrieved_contexts=[row["content"] for row in sample["ranked"][:config["retrieval"]["generation_k"]]],
        )
    value = result.value if isfinite(result.value) and 0 <= result.value <= 1 else None
    report = {"metric": "Ragas Faithfulness 0.4.4", "model": args.model, "group": args.group,
              "question_id": args.question, "value": value, "judge_token_usage": llm.usage,
              "interpretation": "Context-conditioned auxiliary metric on one selected development sample."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Ragas Faithfulness: {value}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--group", choices=tuple("ABCD"), default="B")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=90)
    asyncio.run(main(parser.parse_args()))
