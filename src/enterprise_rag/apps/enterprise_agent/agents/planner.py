"""Native Ollama and LangChain OpenAI tool-call adapters."""

from collections.abc import Callable
import re
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import APIError, APITimeoutError
from pydantic import BaseModel

from enterprise_rag.adapters.models import OllamaEndpoint
from enterprise_rag.config import Settings

Planner = Callable[[list[BaseMessage], list[type[BaseModel]]], AIMessage]
MAX_OUTPUT_TOKENS = 640
CALL_ID = re.compile(r"[a-zA-Z0-9_-]{1,96}")


class PlannerFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _native_message(message: BaseMessage) -> dict:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": message.content}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "content": message.content, "tool_name": message.name,
                "tool_call_id": message.tool_call_id}
    if isinstance(message, AIMessage):
        return {
            "role": "assistant", "content": message.content,
            "tool_calls": [
                {"id": call["id"], "function": {"index": index, "name": call["name"], "arguments": call["args"]}}
                for index, call in enumerate(message.tool_calls)
            ],
        }
    raise ValueError("Unexpected message type")


def _token_count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _native_tools(endpoint: OllamaEndpoint, messages: list[BaseMessage], tools: list[type[BaseModel]]) -> AIMessage:
    reply = endpoint.request("POST", "/api/chat", {
        "stream": False, "think": False,
        "messages": [_native_message(message) for message in messages],
        "tools": [convert_to_openai_tool(tool) for tool in tools],
        "options": {"temperature": 0, "num_predict": MAX_OUTPUT_TOKENS},
    })
    raw = reply["message"]
    calls = []
    invalid = []
    for item in raw.get("tool_calls", []):
        function = item["function"]
        # Ollama native responses can omit an ID. Pair by native call order and tool name.
        call_id = uuid4().hex if item.get("id") is None else item["id"]
        name = function["name"]
        args = function.get("arguments")
        if isinstance(args, dict):
            calls.append({"name": name, "id": call_id, "args": args})
        else:
            invalid.append({"name": name, "id": call_id, "args": None})
    message = AIMessage(content="", tool_calls=calls, invalid_tool_calls=invalid)
    input_count = _token_count(reply.get("prompt_eval_count"))
    output_count = _token_count(reply.get("eval_count"))
    if input_count is not None and output_count is not None:
        message.usage_metadata = {"input_tokens": input_count, "output_tokens": output_count,
                                  "total_tokens": input_count + output_count}
    return message


def create_planner(settings: Settings) -> Planner:
    key = settings.chat_api_key.get_secret_value() if settings.chat_api_key else None
    timeout = min(settings.chat_timeout, 45)
    if settings.chat_provider == "ollama":
        client = OllamaEndpoint(settings.model_url("chat"), settings.chat_model, timeout, key)
    else:
        client = ChatOpenAI(model=settings.chat_model, base_url=settings.model_url("chat"), api_key=settings.chat_api_key,
                            timeout=timeout, max_retries=0, temperature=0, max_completion_tokens=MAX_OUTPUT_TOKENS)

    def plan(messages: list[BaseMessage], tools: list[type[BaseModel]]) -> AIMessage:
        try:
            if isinstance(client, OllamaEndpoint):
                return _native_tools(client, messages, tools)
            with tracing_context(enabled=False):
                output = client.bind_tools(tools, strict=True).invoke(messages)
            if not isinstance(output, AIMessage):
                raise ValueError("Expected an assistant message")
            return output
        except (httpx.TimeoutException, APITimeoutError) as error:
            raise PlannerFailure("model_timeout") from error
        except (httpx.HTTPError, APIError) as error:
            raise PlannerFailure("model_unavailable") from error
        except (KeyError, AttributeError, ValueError, TypeError) as error:
            raise PlannerFailure("model_invalid") from error

    return plan
