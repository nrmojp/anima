"""OpenAI Responses API adapter."""

from __future__ import annotations

import json
import logging
import base64
import mimetypes
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from typing import Any, TYPE_CHECKING

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from anima.core.models import (
    Context,
    DigestJob,
    MemoryDocument,
    ReflectionDraft,
    ReflectionJob,
    ResearchNote,
    ResearchSource,
    ResponseDraft,
    SleepDraft,
    SleepJob,
    Event,
)
from anima.core.telemetry import emit
from anima.core.context_usage import context_usage
from anima.core.external_errors import TransientExternalError
from anima.core.prompts import NAP, REFLECT, RESPOND, SLEEP, PromptSpec
from anima.core.prompts import ADDRESS, REACT
from anima.core.expressions import FACE_NAMES, FACE_GUIDANCE
from anima.capabilities.contracts import (
    CapabilityContext,
    ContextReference,
    FunctionToolSpec,
    NativeToolEvent,
    NativeToolSpec,
    ToolRegistry,
    PermissionSet,
)
from anima.capabilities.responses import ResponseContributionRegistry
from anima.core.sandbox import SandboxKey
from anima.core.self_time import SelfTimeDecision
from anima.core.agentic_loop import (
    AgenticLoop, AgentToolCall, ThoughtStep, ToolObservation,
)


LOGGER = logging.getLogger(__name__)


class OpenAIThoughtBackend:
    """Translate provider-neutral loop turns to Responses API items."""

    def __init__(self, *, base_input, tools, request, observe=None) -> None:
        self.base_input = base_input
        self.tools = tools
        self.request = request
        self.observe = observe
        transcript: list[dict[str, Any]] = []
        self.transcript = transcript

    async def think(
        self, request_count: int, tools_enabled: bool,
        observations: tuple[ToolObservation, ...],
    ) -> ThoughtStep:
        for observation in observations:
            self.transcript.append({
                "type": "function_call_output", "call_id": observation.call.id,
                "output": observation.output,
            })
            for path in observation.attachments:
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if media_type.startswith("image/"):
                    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                    self.transcript.append({
                        "role": "user", "content": [{
                            "type": "input_image",
                            "image_url": f"data:{media_type};base64,{encoded}",
                        }],
                    })
        current_base = (
            self.base_input(request_count) if callable(self.base_input)
            else self.base_input
        )
        response = await self.request(
            request_count, current_base + self.transcript,
            self.tools if tools_enabled else [],
        )
        if self.observe is not None:
            await self.observe(response, request_count)
        calls = tuple(
            AgentToolCall(
                str(item.call_id), str(item.name), str(item.arguments), request_count,
            )
            for item in getattr(response, "output", ())
            if getattr(item, "type", None) == "function_call"
        )
        if calls:
            self.transcript.extend(
                _output_item(item) for item in getattr(response, "output", ())
            )
        return ThoughtStep(calls, None if calls else response)


class OpenAIToolExecutor:
    """Adapt PreparedToolSet execution and telemetry to Core observations."""

    def __init__(self, prepared_tools, *, operation: str, consume=None) -> None:
        self.prepared_tools = prepared_tools
        self.operation = operation
        self.consume = consume

    async def execute(self, call: AgentToolCall) -> ToolObservation:
        tool_started = time.monotonic()
        emit(
            "openai.tool.started", operation=self.operation,
            round=call.round, tool_name=call.name, **_tool_log_fields(call.arguments),
        )
        try:
            result = await self.prepared_tools.execute(call.name, call.arguments)
            output = result.output_json()
        except Exception as error:
            emit(
                "openai.tool.failed", operation=self.operation,
                round=call.round, tool_name=call.name,
                duration_ms=round((time.monotonic() - tool_started) * 1000),
                error_type=type(error).__name__,
            )
            raise
        emit(
            "openai.tool.completed", operation=self.operation,
            round=call.round, tool_name=call.name,
            duration_ms=round((time.monotonic() - tool_started) * 1000),
            **_tool_log_fields(output, result=True),
        )
        if self.consume is not None:
            self.consume(result, call.name)
        return ToolObservation(call, output, result.attachments, result)


if TYPE_CHECKING:
    from anima.core.memory import MemoryRetriever
    from anima.adapters.openai.memory_vector_store import MemoryVectorStore


def normalize_openai_errors(method):
    """Expose connection failures through the SDK-neutral core error contract."""
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except APITimeoutError as error:
            raise TransientExternalError(str(error), reason="timeout") from error
        except APIConnectionError as error:
            raise TransientExternalError(str(error), reason="connection_error") from error

    return wrapped


def _normalize_reply(text: str) -> str:
    """Turn escaped newline sequences from structured output into real lines."""
    return (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("¥n", "\n")
        .replace("￥n", "\n")
    )


def _is_image_download_error(error: Exception) -> bool:
    """Return whether Responses rejected an image URL it could not download."""
    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    message = ""
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            message = str(detail.get("message", ""))
    if not message:
        message = str(error)
    return status_code == 400 and "Error while downloading file" in message


def _input_image_urls(input_items: list[dict[str, Any]]) -> set[str]:
    """Collect image URLs from Responses input."""
    return {
        str(part.get("image_url"))
        for item in input_items
        if isinstance(item.get("content"), list)
        for part in item["content"]
        if isinstance(part, dict)
        and part.get("type") == "input_image"
        and part.get("image_url")
    }


def _without_input_images(
    input_items: list[dict[str, Any]], excluded_urls: set[str] | None = None
) -> list[dict[str, Any]]:
    """Copy Responses input while removing all or selected image URLs."""
    result: list[dict[str, Any]] = []
    for item in input_items:
        content = item.get("content")
        if not isinstance(content, list):
            result.append(item)
            continue
        text_content = [
            part for part in content
            if not isinstance(part, dict)
            or part.get("type") != "input_image"
            or (
                excluded_urls is not None
                and str(part.get("image_url")) not in excluded_urls
            )
        ]
        result.append({**item, "content": text_content})
    return result


def _tool_log_fields(payload: str, *, result: bool = False) -> dict[str, Any]:
    """Extract diagnostic tool metadata without logging lyrics or full payloads."""
    try:
        value = json.loads(payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"payload_valid": False}
    if not isinstance(value, dict):
        return {"payload_valid": False}
    fields: dict[str, Any] = {"payload_valid": True}
    for key in ("query", "identifier", "theme", "error", "due_at", "reminder_id"):
        if key in value:
            fields[key] = str(value[key])[:200]
    for key in ("limit", "include_lyrics", "percent", "ok", "playing"):
        if key in value:
            fields[key] = value[key]
    if result and isinstance(value.get("results"), list):
        fields["result_count"] = len(value["results"])
        fields["result_ids"] = [
            str(item["id"])
            for item in value["results"][:3]
            if isinstance(item, dict) and item.get("id") is not None
        ]
    track = value.get("track", value) if result else value
    if isinstance(track, dict) and track.get("id") is not None:
        fields["track_id"] = str(track["id"])
    return fields


def _load_first_json_object(payload: str, *, operation: str) -> dict[str, Any]:
    """Decode one structured object and tolerate provider-appended trailing text."""
    value, end = json.JSONDecoder().raw_decode(payload.lstrip())
    if not isinstance(value, dict):
        raise TypeError(f"{operation} response is not an object")
    if payload.lstrip()[end:].strip():
        emit("openai.structured_output.trailing_data", operation=operation)
    return value


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "minLength": 1},
        "research_summary": {"type": ["string", "null"], "maxLength": 600},
        "face": {"type": "string", "enum": list(FACE_NAMES)},
        "mood": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "maxLength": 40},
                "cause": {"type": "string", "maxLength": 40},
                "strength": {"type": "string", "enum": ["弱い", "ふつう", "強い"]},
                "focus": {"type": "string", "maxLength": 60},
            },
            "required": ["state", "cause", "strength", "focus"],
            "additionalProperties": False,
        },
    },
    "required": ["reply", "research_summary", "mood", "face"],
    "additionalProperties": False,
}

DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {
                        "type": "string",
                        "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    },
                    "place": {
                        "type": "string",
                        "pattern": "^(?:DM|#[^\\]\\s]+)$",
                    },
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["time", "place", "content"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entries"],
    "additionalProperties": False,
}

SLEEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["self", "world", "channel", "person"],
                    },
                    "key": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"},
                    "heading": {"type": ["string", "null"]},
                    "relationship": {"type": ["string", "null"]},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                                "place": {"type": "string", "pattern": "^(?:DM|#[^\\]\\s]+)$"},
                                "source": {"type": ["string", "null"]},
                                "content": {"type": "string", "minLength": 1},
                                "strong": {"type": "boolean"},
                            },
                            "required": ["date", "place", "source", "content", "strong"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["scope", "key", "heading", "relationship", "entries"],
                "additionalProperties": False,
            },
        },
        "open_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "place": {"type": "string", "pattern": "^(?:DM|#[^\\]\\s]+)$"},
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["date", "place", "content"],
                "additionalProperties": False,
            },
        },
        "mood": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "maxLength": 40},
                "cause": {"type": "string", "maxLength": 40},
                "strength": {"type": "string", "enum": ["弱い", "ふつう", "強い"]},
                "focus": {"type": "string", "maxLength": 60},
            },
            "required": ["state", "cause", "strength", "focus"],
            "additionalProperties": False,
        },
    },
    "required": ["memories", "open_items", "mood"],
    "additionalProperties": False,
}

REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "changed": {"type": "boolean"},
        "habits": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 10,
        },
        "conflict": {"type": ["string", "null"]},
    },
    "required": ["changed", "habits", "conflict"],
    "additionalProperties": False,
}


class OpenAIAddressClassifier:
    def __init__(self, *, client, model):
        self.client, self.model = client, model

    @normalize_openai_errors
    async def expects_reply(self, snapshot, history, event):
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["expects_reply"],
                  "properties": {"expects_reply": {"type": "boolean"}}}
        started = time.monotonic()
        emit("openai.request.started", operation="address", **ADDRESS.log_fields(self.model))
        response = await self.client.responses.create(
            model=self.model, store=False, reasoning={"effort": ADDRESS.effort},
            instructions=ADDRESS.instructions,
            input=json.dumps({"persona": snapshot.persona[:2000], "target": event.id,
                              "events": [{"id": e.id, "author_id": e.author_id,
                                          "name": e.author_name, "reply_to": e.reply_to,
                                          "response_to": e.response_to,
                                          "reply_author_name": e.reply_author_name,
                                          "reply_text": (e.reply_text or "")[:1000],
                                          "called_name": e.called_name,
                                          "text": e.text[:1000]}
                                         for e in (*history, event)]}, ensure_ascii=False),
            max_output_tokens=256,
            text={"format": {"type": "json_schema", "name": "anima_address",
                             "strict": True, "schema": schema}},
        )
        OpenAIMemoryMaintainer._emit_usage(response, "address", started, ADDRESS, self.model)
        value = json.loads(response.output_text)["expects_reply"]
        if type(value) is not bool:
            raise ValueError("address decision must be boolean")
        return value


class OpenAIReactionClassifier:
    def __init__(self, *, client, model):
        self.client, self.model = client, model

    @normalize_openai_errors
    async def classify(
        self, snapshot, events, available_faces, *, allow_react=True, allow_speak=False
    ):
        actions = [
            "none",
            *(["react"] if allow_react else []),
            *(["speak"] if allow_speak else []),
        ]
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["action", "target", "face"],
            "properties": {
                "action": {"type": "string", "enum": actions},
                "target": {"type": ["string", "null"], "enum": [None, *(e.id for e in events)]},
                "face": {"type": ["string", "null"], "enum": [None, *available_faces]},
            },
        }
        started = time.monotonic()
        emit("openai.request.started", operation="react", **REACT.log_fields(self.model))
        response = await self.client.responses.create(
            model=self.model, store=False, reasoning={"effort": REACT.effort},
            instructions=(
                REACT.instructions
                + ("\n特に自然に会話へ加わる価値が高い場合だけspeakを選ぶ。"
                   "speakではtargetを1つ選び、faceはnullにする。" if allow_speak else "")
                + "\n" + FACE_GUIDANCE
            ),
            input=json.dumps({
                "persona": snapshot.persona[:4000], "habitus": snapshot.habitus[:2000],
                "mood": snapshot.mood.to_dict(), "available_faces": available_faces,
                "events": [{
                    "id": e.id, "who": e.author_name[:80], "bot": e.author_is_bot,
                    "text": e.text[:500],
                } for e in events[-20:]],
            }, ensure_ascii=False),
            max_output_tokens=256,
            text={"format": {"type": "json_schema", "name": "anima_reaction", "strict": True, "schema": schema}},
        )
        OpenAIMemoryMaintainer._emit_usage(response, "react", started, REACT, self.model)
        return json.loads(response.output_text)


class OpenAISelfTimeDecider:
    """Decide and perform a small, allowlisted autonomous tool loop."""

    SAFE_TOOLS = frozenset({
        "resource_list", "resource_search", "resource_read", "resource_write",
        "resource_transfer", "resource_delete",
    })
    SAFE_NATIVE_TOOLS = frozenset({"web_search"})

    def __init__(
        self, *, client, model, tool_registry: ToolRegistry | None = None,
        sandbox_key: SandboxKey | None = None, max_tool_rounds: int = 10,
        allowed_action_tools: frozenset[str] = frozenset(),
    ):
        if max_tool_rounds < 1:
            raise ValueError("self-time tool round limit must be positive")
        self.client, self.model = client, model
        self.tool_registry, self.sandbox_key = tool_registry, sandbox_key
        self.max_tool_rounds = max_tool_rounds
        self.allowed_action_tools = allowed_action_tools

    def _source(self, iteration: int) -> Event:
        if self.sandbox_key is None:
            raise RuntimeError("self-time tools require a sandbox key")
        now = datetime.now(timezone.utc)
        kind = "channel" if self.sandbox_key.kind == "guild" else "dm"
        return Event(
            id=f"selftime-{int(now.timestamp())}-{now.microsecond}-{iteration}", ts=now, kind=kind,
            channel_id=self.sandbox_key.id, channel_name="self-time",
            author_id="self", author_name="self", text="",
            guild_id=self.sandbox_key.id if kind == "channel" else None,
            sandbox_key=str(self.sandbox_key), author_is_bot=True,
        )

    @normalize_openai_errors
    async def decide(self, context, iteration, previous):
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["action", "note", "direction", "discovery", "mood_state", "mood_cause", "mood_strength", "mood_focus", "reflection"],
            "properties": {
                "action": {"type": "string", "enum": ["none", "continue", "finish"]},
                "note": {"type": "string", "maxLength": 500,
                         "description": "noneでは空文字。continue/finishでは空白のみ不可、実際の活動を1〜500文字で記録。"},
                "direction": {"type": "string", "enum": ["deepen", "broaden"]},
                "discovery": {"type": "string", "minLength": 1, "maxLength": 500},
                "mood_state": {"type": "string", "maxLength": 40},
                "mood_cause": {"type": "string", "maxLength": 40},
                "mood_strength": {"type": "string", "enum": ["弱い", "ふつう", "強い"]},
                "mood_focus": {"type": "string", "maxLength": 60},
                "reflection": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        }
        prepared = None
        tools = []
        allowed_tools = set(self.SAFE_TOOLS)
        if self.tool_registry is not None:
            if self.sandbox_key is None:
                raise RuntimeError("self-time tool registry requires a sandbox key")
            prepared = await self.tool_registry.prepare(CapabilityContext(
                self._source(iteration), self.sandbox_key,
                PermissionSet(frozenset({"self_time", "resource.delete_temporary"})),
            ))
            allowed_tools.update(self.allowed_action_tools)
            tools = [
                _openai_tool_spec(spec) for spec in prepared.specs
                if (
                    isinstance(spec, FunctionToolSpec) and spec.name in allowed_tools
                ) or (
                    isinstance(spec, NativeToolSpec) and spec.kind in self.SAFE_NATIVE_TOOLS
                )
            ]
        instructions = (
            "これは話しかけられていない自分自身の時間。入力の状態を眺め、"
            "いま自然に気になるものを一度探してから行動するか判断する。"
            "探索には二つの方向がある。deepenは今ある関心や未完了事項を一歩深める。"
            "broadenは会話・記憶・持ち物を手がかりに、最近扱っていない別の関心とのつながりを探す。"
            "どちらへ進むかを自分で選びdirectionに記録する。"
            "直近の履歴を見て進展なく同じ対象を眺めるだけになりそうなら、"
            "別の関心を探すbroadenも検討する。ただしランダムな話題探しはしない。"
            "選んだ方向で何を確かめ何が分かったかをdiscoveryに短く記録する。"
            "手がかりが得られなかった時も、その結果を正直に記録する。"
            "気分、記憶、未完了事項、持ち物を組み合わせ、新しい関心や続きが見つかったなら、"
            "小さく安全な活動を自分で選んでよい。義務的に行動する必要はなく、"
            "関心が生まれなかった時はnoneを選んでよい。"
            "何かを実行する時は必ず提供されたツールを使い、実行結果を見てから判断する。"
            "文章を作る場合はcore.inventoryへresource_writeで保存できる。"
            "状態に判断待ちの一時成果物がある場合はcore.temporary_artifactsを確認する。"
            "一時成果物を残す場合は、まだ内容を観察していなければresource_readで確認し、"
            "resource_transferでcore.inventoryへ移す。"
            "不要な一時成果物だけはresource_deleteで破棄できる。inventoryは削除できない。"
            "後日したいことが本当に生じた場合だけcore.open_itemsのopen.mdへappendする。"
            "open.mdは未完了事項のリスト。整理するときはresource_readで最新の全文を確認し、"
            "resource_writeのreplaceで未完了の行を残して更新できる。全て完了なら空文字にできる。"
            "完了の根拠がある項目だけ取り除き、関係ない約束は残す。"
            "各行の既存の日付・場を保持し、(job:...)付きの行は一切変更しない。"
            "完了したことはnoteに記録し、同じ予定を重複追加しない。"
            "選んだ関心を調べるために手元の情報が足りない時はWeb検索を使ってよい。"
            "目的なく話題を探し回らず、現在の関心や過去の活動につながる検索を優先する。"
            "外部で行っていない活動やツールが成功していない活動をnoteへ捏造しない。"
            "noteは実際に考えたこと・実行したこと・失敗を短く過去形で書く。"
            "continue/finishのnoteは空白だけにせず1〜500文字。noneのnoteは空文字。"
            "reflectionはダッシュボードで本人に見せる思考の要約。入力から何に目を留め、"
            "どう判断したかを一人称で簡潔に書く。noneでも空にしない。"
            "continueは別の反復が本当に必要な場合だけ。最大回数を使い切ろうとしない。"
            "recent_self_timeは直近の活動履歴、current_self_timeは今回ここまでの判断。"
            "日時・思考要約・実行記録・終了状態を確認し、済んだ活動を未実施として扱わない。"
            "失敗や中断の記録は成功の証拠ではないため、必要なら資源を確認する。"
            "同じ関心を深めてもよいが、進展なく同じ行動や先送りを繰り返さない。"
            "未完了事項は実行済みなら更新・完了整理し、同じ事項を再登録しない。"
            "新たな進展がなければnoneで終えてよい。"
        )
        if prepared is not None and prepared.contextual_instructions:
            instructions += "\n\n" + prepared.contextual_instructions
        state_input = json.dumps({
            "iteration": iteration, "previous": asdict(previous) if previous else None,
            "state": context,
        }, ensure_ascii=False, default=str)
        base_input = [{
            "role": "user", "content": [{"type": "input_text", "text": state_input}],
        }]
        reasoning_summaries: list[str] = []
        async def request(request_count, input_items, round_tools):
            started = time.monotonic()
            emit(
                "openai.request.started", operation="self_time", model=self.model,
                round=request_count, tools_enabled=bool(round_tools),
            )
            request_options = {
                "model": self.model, "store": False,
                "reasoning": {"effort": "low", "summary": "auto"},
                "instructions": instructions, "input": input_items,
                "tools": round_tools, "max_output_tokens": 900,
                "text": {"format": {"type": "json_schema", "name": "anima_self_time",
                                     "strict": True, "schema": schema}},
            }
            if any(tool.get("type") == "web_search" for tool in round_tools):
                request_options["include"] = ["web_search_call.action.sources"]
            emit("model.context.measured", operation="self_time", model=self.model,
                 round=request_count, **context_usage(request_options, prepared))
            response = await self.client.responses.create(
                **request_options,
            )
            usage = getattr(response, "usage", None)
            emit(
                "openai.response.completed", operation="self_time", model=self.model,
                round=request_count,
                duration_ms=round((time.monotonic() - started) * 1000),
                response_id=getattr(response, "id", None),
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
            )
            summary = _reasoning_summary_text(response)
            if summary:
                reasoning_summaries.append(summary)
            if prepared is not None:
                native_events = tuple(
                    _native_tool_event(item, "web_search", request_count, item_index)
                    for item_index, item in enumerate(getattr(response, "output", ()))
                    if getattr(item, "type", None) == "web_search_call"
                )
                for result in await prepared.consume_native(native_events):
                    emit(
                        "openai.tool.completed", operation="self_time",
                        round=request_count, tool_name="web_search",
                        reference_count=len(result.references),
                    )
            return response

        backend = OpenAIThoughtBackend(
            base_input=base_input, tools=tools, request=request,
        )
        executor = OpenAIToolExecutor(prepared, operation="self_time") if prepared else None
        loop = await AgenticLoop(max_tool_rounds=self.max_tool_rounds).run(
            backend=backend, executor=executor,
        )
        response = loop.result
        if not response.output_text:
            raise RuntimeError("self-time response did not contain a decision")
        for attempt in range(2):
            decision = {}
            try:
                decision = _load_first_json_object(response.output_text, operation="self_time")
                if any(not isinstance(value, str) for value in decision.values()):
                    raise ValueError("self-time decision fields must be strings")
                if reasoning_summaries:
                    decision["reflection"] = "\n\n".join(reasoning_summaries)[:500]
                if decision.get("action") == "none":
                    decision["note"] = ""
                result = SelfTimeDecision(**decision)
            except (ValueError, TypeError) as error:
                reason = str(error) if isinstance(error, ValueError) else "invalid decision fields or types"
                emit("self_time.decision.invalid", attempt=attempt + 1,
                     response_id=getattr(response, "id", None),
                     error_type=type(error).__name__, reason=reason,
                     action=decision.get("action") if decision.get("action") in ("none", "continue", "finish") else "invalid",
                     field_lengths={key: len(value) for key, value in decision.items() if isinstance(value, str)},
                     retrying=attempt == 0)
                LOGGER.warning("Invalid self-time decision: attempt=%s reason=%s lengths=%s",
                               attempt + 1, reason,
                               {key: len(value) for key, value in decision.items() if isinstance(value, str)})
                if attempt:
                    raise
                # Repair only the record, preserving observations but never replaying tools.
                response = await request(loop.request_count + 1, base_input + backend.transcript + [
                    {"role": "assistant", "content": response.output_text},
                    {"role": "user", "content": "最終記録の検証失敗: " + reason +
                     "。ツールは再実行せず、既存の実行結果だけに基づいてJSONを修正してください。"
                     "continue/finishのnoteは空白のみ不可、1〜500文字。noneでは空文字。"},
                ], [])
            else:
                if attempt:
                    emit("self_time.decision.repaired", response_id=getattr(response, "id", None))
                return result


def _reasoning_summary_text(response: object) -> str:
    """Extract only the API-provided summary, never opaque reasoning tokens."""
    parts: list[str] = []
    for item in getattr(response, "output", ()) or ():
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if item_type != "reasoning":
            continue
        summary = item.get("summary", ()) if isinstance(item, dict) else getattr(item, "summary", ())
        for entry in summary or ():
            text = entry.get("text") if isinstance(entry, dict) else getattr(entry, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


class OpenAIResponder:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        enable_web_search: bool = False,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        client: AsyncOpenAI | None = None,
        tool_registry: ToolRegistry | None = None,
        sandbox_key: SandboxKey | None = None,
        memory_vector_store: MemoryVectorStore | None = None,
        memory_retriever: MemoryRetriever | None = None,
        appearance: str = "",
        response_registry: ResponseContributionRegistry | None = None,
    ) -> None:
        self.client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )
        self.model = model
        self.enable_web_search = enable_web_search
        self.tool_registry = tool_registry
        self.sandbox_key = sandbox_key
        self.memory_vector_store = memory_vector_store
        self.memory_retriever = memory_retriever
        self.appearance = appearance.strip()
        self.response_registry = response_registry or ResponseContributionRegistry()
        self._unavailable_image_urls: set[str] = set()

    def reload_configuration(self, *, appearance: str) -> None:
        """Replace fixed persona resources retained by this adapter."""
        value = appearance.strip()
        if not value:
            raise ValueError("appearance must not be empty")
        self.appearance = value

    @normalize_openai_errors
    async def respond(self, context: Context) -> ResponseDraft:
        started = time.monotonic()
        tools: list[dict[str, Any]] = []
        legacy_native_tools = self.tool_registry is None
        if legacy_native_tools and self.enable_web_search:
            tools.append({"type": "web_search"})
        prepared_tools = None
        prepared_response = None
        if self.tool_registry is not None:
            key = (
                SandboxKey.for_event(context.source_event)
                if context.source_event is not None
                else self.sandbox_key
            )
            if key is None:
                raise RuntimeError("tool registry requires a sandbox key")
            prepared_tools = await self.tool_registry.prepare(
                CapabilityContext(context.source_event, key)
            )
            tools.extend(_openai_tool_spec(spec) for spec in prepared_tools.specs)
            prepared_response = self.response_registry.prepare(
                CapabilityContext(context.source_event, key)
            )
        vector_store_id = None
        if self.memory_vector_store is not None:
            sync_started = time.monotonic()
            try:
                vector_store_id = await self.memory_vector_store.ensure()
                emit(
                    "memory.vector_store.ready",
                    operation="respond",
                    available=bool(vector_store_id),
                    duration_ms=round((time.monotonic() - sync_started) * 1000),
                )
            except Exception as error:
                emit(
                    "memory.vector_store.failed",
                    operation="respond",
                    duration_ms=round((time.monotonic() - sync_started) * 1000),
                    error_type=type(error).__name__,
                )
        if vector_store_id:
            tools.append({
                "type": "file_search",
                "vector_store_ids": [vector_store_id],
                "max_num_results": 5,
            })
        local_recall = ""
        if not vector_store_id and self.memory_retriever is not None:
            from anima.core.memory import MemoryQuery
            source = context.source_event
            recall = await self.memory_retriever.prepare_recall(MemoryQuery(
                text=_recall_query(context),
                person_id=source.response_person_id if source is not None else None,
                conversation_id=source.channel_id if source is not None else None,
            ))
            if recall.passages:
                local_recall = "\n\n# 関連して思い出したこと\n" + "\n\n".join(
                    f"[{passage.source}]\n{passage.text}" for passage in recall.passages
                )
        includes = []
        web_search_enabled = any(tool.get("type") == "web_search" for tool in tools)
        if web_search_enabled:
            includes.append("web_search_call.action.sources")
        if vector_store_id:
            includes.append("file_search_call.results")
        input_items = _without_input_images(
            list(context.input), self._unavailable_image_urls
        )
        acts: list[str] = []
        capability_actions = []
        research_queries: list[str] = []
        research_sources: list[ResearchSource] = []
        input_tokens = output_tokens = total_tokens = 0
        response = None
        request_count = 0
        tool_call_count = 0
        capability_references: list[ContextReference] = []
        image_paths = []
        async def request(round_number, round_input, round_tools):
            nonlocal input_tokens, output_tokens, total_tokens
            round_input = _without_input_images(
                round_input, self._unavailable_image_urls
            )
            emit(
                "openai.request.started",
                operation="respond",
                round=round_number,
                compact_context=round_number > 1,
                tools_enabled=bool(round_tools),
                **RESPOND.log_fields(self.model),
            )
            contextual_instructions = (
                context.instructions
                if round_number == 1
                else _compact_tool_instructions(context.instructions)
            )
            request = {
                "model": self.model,
                "instructions": (
                    contextual_instructions + local_recall
                    + (
                        "\n\n" + prepared_tools.contextual_instructions
                        if prepared_tools is not None
                        and prepared_tools.contextual_instructions else ""
                    )
                    + "\n\n"
                    + FACE_GUIDANCE
                    + "\n"
                    + RESPOND.instructions
                ),
                "input": round_input,
                "store": False,
                "reasoning": {"effort": RESPOND.effort},
                "text": {"format": (
                    prepared_response.compose_format(RESPONSE_SCHEMA)
                    if prepared_response is not None
                    else {
                        "type": "json_schema", "name": "anima_response",
                        "strict": True, "schema": RESPONSE_SCHEMA,
                    }
                )},
                "tools": round_tools,
                "parallel_tool_calls": False,
                **({"include": includes} if includes else {}),
            }
            emit("model.context.measured", operation="respond", model=self.model,
                 round=round_number, **context_usage(request, prepared_tools, prepared_response))
            try:
                current_response = await self.client.responses.create(**request)
            except Exception as error:
                text_only_input = _without_input_images(round_input)
                if not _is_image_download_error(error) or text_only_input == round_input:
                    raise
                self._unavailable_image_urls.update(_input_image_urls(round_input))
                emit(
                    "openai.image.unavailable",
                    operation="respond",
                    round=round_number,
                    error_type=type(error).__name__,
                )
                current_response = await self.client.responses.create(
                    **{**request, "input": text_only_input}
                )
            usage = getattr(current_response, "usage", None)
            round_input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            round_output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            round_total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            input_tokens += round_input_tokens
            output_tokens += round_output_tokens
            total_tokens += round_total_tokens
            emit(
                "openai.response.round_completed",
                operation="respond",
                round=round_number,
                compact_context=round_number > 1,
                input_tokens=round_input_tokens,
                output_tokens=round_output_tokens,
                total_tokens=round_total_tokens,
            )
            return current_response

        async def observe(current_response, round_number):
            native_events = []
            for item_index, item in enumerate(current_response.output):
                if getattr(item, "type", None) == "web_search_call":
                    if legacy_native_tools:
                        acts.append("web_search")
                    query, sources = _web_search_metadata(item)
                    if query and query not in research_queries and len(research_queries) < 3:
                        research_queries.append(query)
                    for source in sources:
                        if source.url not in {known.url for known in research_sources}:
                            research_sources.append(source)
                        if len(research_sources) >= 3:
                            break
                    native_events.append(_native_tool_event(
                        item, "web_search", round_number, item_index
                    ))
                elif getattr(item, "type", None) == "file_search_call":
                    acts.append("file_search")
                elif getattr(item, "type", None) == "image_generation_call":
                    native_events.append(_native_tool_event(
                        item, "image_generation", round_number, item_index
                    ))
                    emit(
                        "image_generation.completed",
                        event_id=(context.source_event.id if context.source_event else None),
                        operation="respond",
                        round=round_number,
                        model=str(getattr(item, "model", "unknown")),
                        image_count=1,
                    )
            if prepared_tools is not None and native_events:
                for result in await prepared_tools.consume_native(native_events):
                    acts.extend(action.action for action in result.actions)
                    capability_actions.extend(result.actions)
                    capability_references.extend(result.references)
                    image_paths.extend(result.attachments)

        def consume(result, _name):
            acts.extend(action.action for action in result.actions)
            capability_actions.extend(result.actions)
            capability_references.extend(result.references)
            if _name != "resource_read" or result.model_payload.get("attach_to_reply") is True:
                image_paths.extend(result.attachments)
            if prepared_response is not None:
                prepared_response.observe_tool_result(result)

        backend = OpenAIThoughtBackend(
            base_input=lambda number: (
                input_items if number == 1 else _compact_tool_input(context.input)
            ), tools=tools,
            request=request, observe=observe,
        )
        executor = (
            OpenAIToolExecutor(prepared_tools, operation="respond", consume=consume)
            if prepared_tools is not None else None
        )
        loop = await AgenticLoop(max_tool_rounds=4).run(
            backend=backend, executor=executor,
        )
        response = loop.result
        request_count = loop.request_count
        tool_call_count = loop.tool_call_count
        if response is None or not response.output_text:
            raise RuntimeError("OpenAI response did not contain output text after tools")
        value = json.loads(response.output_text)
        if prepared_response is not None:
            _reply, contribution_results = await prepared_response.consume_value(
                value, allowed_core=frozenset(RESPONSE_SCHEMA["properties"]),
            )
            for result in contribution_results:
                acts.extend(action.action for action in result.actions)
                capability_actions.extend(result.actions)
                capability_references.extend(result.references)
                image_paths.extend(result.attachments)
        mood = value["mood"]
        research = None
        if "web_search" in acts:
            summary = " ".join(str(value.get("research_summary") or value["reply"]).split())[:600]
            research = ResearchNote(
                summary=summary,
                queries=tuple(research_queries),
                sources=tuple(research_sources),
            )
        draft = ResponseDraft(
            face=value.get("face", "ふつう"),
            reply=_normalize_reply(value["reply"]),
            mood_state=mood["state"],
            mood_cause=mood["cause"],
            mood_strength=mood["strength"],
            mood_focus=mood["focus"],
            acts=tuple(acts),
            response_id=getattr(response, "id", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            research=research,
            images=tuple(image_paths),
            actions=tuple(capability_actions),
            references=tuple(capability_references),
        )
        emit(
            "openai.response.completed",
            operation="respond",
            **RESPOND.log_fields(self.model),
            duration_ms=round((time.monotonic() - started) * 1000),
            response_id=draft.response_id,
            input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens,
            total_tokens=draft.total_tokens,
            request_count=request_count,
            tool_call_count=tool_call_count,
        )
        if prepared_response is not None:
            draft = self.response_registry.enrich_draft(draft, prepared_response.context)
        return draft


def _native_tool_event(item: Any, kind: str, round_index: int, item_index: int) -> NativeToolEvent:
    call_id = str(getattr(item, "id", None) or f"{kind}:{round_index}:{item_index}")
    status = "failed" if getattr(item, "status", None) == "failed" else "completed"
    if kind == "web_search":
        query, sources = _web_search_metadata(item)
        payload: dict[str, object] = {
            "query": query or "",
            "sources": [{"title": source.title, "url": source.url} for source in sources],
        }
    else:
        payload = {"result": str(getattr(item, "result", ""))}
    return NativeToolEvent(kind, call_id, status, payload)


def _recall_query(context: Context) -> str:
    parts = []
    if context.source_event is not None:
        parts.append(context.source_event.text)
    for item in reversed(context.input):
        if item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(
                str(value.get("text", "")) for value in content
                if isinstance(value, dict) and value.get("type") == "input_text"
            )
        if len(parts) >= 4:
            break
    return "\n".join(parts[:4])


def _openai_tool_spec(spec: FunctionToolSpec | NativeToolSpec) -> dict[str, Any]:
    if isinstance(spec, FunctionToolSpec):
        return {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "strict": spec.strict,
            "parameters": dict(spec.parameters),
        }
    return {"type": spec.kind, **dict(spec.options)}


def _output_item(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    return {
        key: getattr(item, key)
        for key in ("type", "name", "call_id", "arguments")
        if hasattr(item, key)
    }


def _compact_tool_input(items: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Keep the current conversational turn while tool outputs replace old history."""
    return list(items[-4:])


def _compact_tool_instructions(instructions: str, *, limit: int = 3200) -> str:
    """Drop bulky memory while retaining the current speaker's identity header."""
    if len(instructions) <= limit:
        return instructions
    omission = "\n\n[中間の長期記憶はツール往復中のため省略]\n\n"
    author_marker = "\n\n# いま話している相手\n"
    marker_index = instructions.rfind(author_marker)
    if marker_index >= 0:
        # The preferred form of address is near the start of this final section.
        # Taking the raw tail can cut that header off when a person's memory is long.
        author = instructions[marker_index + 2:][:1200].rstrip()
        target_start = instructions.rfind("\n\n# 今回の応答対象\n", 0, marker_index)
        if target_start >= 0:
            author = instructions[target_start + 2:marker_index].rstrip() + "\n\n" + author
        head_limit = max(0, limit - len(omission) - len(author))
        return instructions[:head_limit].rstrip() + omission + author
    head = instructions[:2200].rstrip()
    tail = instructions[-800:].lstrip()
    return head + omission + tail


def _web_search_metadata(item: Any) -> tuple[str | None, tuple[ResearchSource, ...]]:
    action = getattr(item, "action", None)
    query = getattr(action, "query", None)
    if query is not None:
        query = " ".join(str(query).split())[:200] or None
    result: list[ResearchSource] = []
    for value in getattr(action, "sources", ()) or ():
        title = " ".join(str(getattr(value, "title", "") or "").split())[:200]
        url = str(getattr(value, "url", "") or "")
        try:
            result.append(ResearchSource(title=title or url[:200], url=url))
        except ValueError:
            continue
        if len(result) == 3:
            break
    return query, tuple(result)


class OpenAIMemoryMaintainer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reflection_model: str | None = None,
        timeout_seconds: float = 300.0,
        max_retries: int = 2,
        memory_strong_max: int = 10,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if memory_strong_max < 0:
            raise ValueError("memory_strong_max must be non-negative")
        self.client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )
        self.model = model
        self.reflection_model = reflection_model or model
        self.memory_strong_max = memory_strong_max

    @normalize_openai_errors
    async def digest(self, job: DigestJob) -> str:
        started = time.monotonic()
        emit("openai.request.started", operation="digest", **NAP.log_fields(self.model))
        response = await self.client.responses.create(
            model=self.model,
            instructions=NAP.instructions,
            input=json.dumps(
                {
                    "existing_digest": job.existing_digest,
                    "events": [event.to_log_dict() for event in job.events],
                },
                ensure_ascii=False,
            ),
            store=False,
            reasoning={"effort": NAP.effort},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "anima_digest",
                    "strict": True,
                    "schema": DIGEST_SCHEMA,
                }
            },
        )
        value = self._json_output(response, "digest")
        self._emit_usage(response, "digest", started, NAP, self.model)
        return self._digest_text(value["entries"])

    @staticmethod
    def _digest_text(entries: list[dict[str, Any]]) -> str:
        """Render model data into the durable Markdown format mechanically."""
        lines: list[str] = []
        for entry in entries:
            clock = str(entry["time"]).strip()
            place = str(entry["place"]).strip()
            content = " ".join(str(entry["content"]).split())
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
                raise ValueError(f"invalid digest time: {clock}")
            if not re.fullmatch(r"(?:DM|#[^\]\s]+)", place):
                raise ValueError(f"invalid digest place: {place}")
            if not content:
                raise ValueError("digest content must not be empty")
            lines.append(f"- [{clock} {place}] {content}")
        if not lines:
            raise ValueError("digest entries must not be empty")
        return "\n".join(lines)

    @normalize_openai_errors
    async def sleep(self, job: SleepJob) -> SleepDraft:
        started = time.monotonic()
        emit("openai.request.started", operation="sleep", **SLEEP.log_fields(self.model))
        response = await self.client.responses.create(
            model=self.model,
            instructions=SLEEP.instructions,
            input=json.dumps(self._sleep_input(job), ensure_ascii=False),
            store=False,
            reasoning={"effort": SLEEP.effort},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "anima_sleep",
                    "strict": True,
                    "schema": SLEEP_SCHEMA,
                }
            },
        )
        value = self._json_output(response, "sleep")
        self._emit_usage(response, "sleep", started, SLEEP, self.model)
        mood = value["mood"]
        memory_documents = self._merge_memory_documents(value["memories"])
        return SleepDraft(
            memories=tuple(
                MemoryDocument(
                    scope=document["scope"],
                    key=document["key"],
                    content=self._memory_text(document, self.memory_strong_max),
                )
                for document in memory_documents
            ),
            open_items=self._open_text(value["open_items"]),
            mood_state=mood["state"],
            mood_cause=mood["cause"],
            mood_strength=mood["strength"],
            mood_focus=mood["focus"],
        )

    @staticmethod
    def _merge_memory_documents(
        documents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge duplicate sleep documents without discarding distinct memories."""
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        duplicate_count = 0
        for document in documents:
            identity = (document["scope"], document["key"])
            current = merged.get(identity)
            if current is None:
                merged[identity] = {
                    **document,
                    "entries": list(document["entries"]),
                }
                continue
            duplicate_count += 1
            for field in ("heading", "relationship"):
                if document[field] is not None:
                    current[field] = document[field]
            for entry in document["entries"]:
                if entry not in current["entries"]:
                    current["entries"].append(entry)
        if duplicate_count:
            emit(
                "memory.sleep_duplicates_merged",
                duplicate_count=duplicate_count,
                document_count=len(merged),
            )
        return list(merged.values())

    @normalize_openai_errors
    async def reflect(self, job: ReflectionJob) -> ReflectionDraft:
        started = time.monotonic()
        emit("openai.request.started", operation="reflection", **REFLECT.log_fields(self.reflection_model))
        response = await self.client.responses.create(
            model=self.reflection_model,
            instructions=REFLECT.instructions,
            input=json.dumps(
                {
                    "persona": job.persona,
                    "current_habitus": job.habitus,
                    "self_memory": job.self_memory,
                },
                ensure_ascii=False,
            ),
            store=False,
            reasoning={"effort": REFLECT.effort},
            text={
                "format": {
                    "type": "json_schema",
                    "name": "anima_reflection",
                    "strict": True,
                    "schema": REFLECTION_SCHEMA,
                }
            },
        )
        value = self._json_output(response, "reflection")
        self._emit_usage(response, "reflection", started, REFLECT, self.reflection_model)
        return ReflectionDraft(
            changed=bool(value["changed"]),
            habitus=self._habitus_text(value["habits"]),
            conflict=value["conflict"],
        )

    @staticmethod
    def _one_line(value: Any, field: str) -> str:
        text = " ".join(str(value).split())
        if not text:
            raise ValueError(f"{field} must not be empty")
        return text

    @classmethod
    def _memory_text(
        cls, document: dict[str, Any], strong_max: int = 10
    ) -> str:
        if strong_max < 0:
            raise ValueError("strong_max must be non-negative")
        lines: list[str] = []
        strong_count = 0
        if document["heading"] is not None:
            lines.append(f"## {cls._one_line(document['heading'], 'memory heading')}")
        if document["relationship"] is not None:
            lines.append(f"関係: {cls._one_line(document['relationship'], 'memory relationship')}")
        for entry in document["entries"]:
            date = cls._date(entry["date"], "memory date")
            place = cls._place(entry["place"], "memory place")
            source = (
                f" {cls._one_line(entry['source'], 'memory source')}"
                if entry["source"] is not None else ""
            )
            content = re.sub(
                r"\s*※強\s*$", "", cls._one_line(entry["content"], "memory content")
            )
            is_strong = bool(entry["strong"]) and strong_count < strong_max
            strong_count += int(is_strong)
            strong = " ※強" if is_strong else ""
            lines.append(f"- [{date} {place}{source}] {content}{strong}")
        return "\n".join(lines)

    @classmethod
    def _open_text(cls, entries: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"- [{cls._date(entry['date'], 'open date')} "
            f"{cls._place(entry['place'], 'open place')}] "
            f"{cls._one_line(entry['content'], 'open content')}"
            for entry in entries
        )

    @classmethod
    def _habitus_text(cls, habits: list[str]) -> str:
        return "\n".join(f"- {cls._habit(item)}" for item in habits)

    @classmethod
    def _habit(cls, value: Any) -> str:
        return re.sub(r"^-\s*", "", cls._one_line(value, "habit"))

    @staticmethod
    def _date(value: Any, field: str) -> str:
        text = str(value).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(f"invalid {field}: {text}")
        return text

    @staticmethod
    def _place(value: Any, field: str) -> str:
        text = str(value).strip()
        if not re.fullmatch(r"(?:DM|#[^\]\s]+)", text):
            raise ValueError(f"invalid {field}: {text}")
        return text

    @staticmethod
    def _sleep_input(job: SleepJob) -> dict[str, Any]:
        memories = [
            {"scope": "self", "key": "self", "content": job.self_memory},
            {"scope": "world", "key": "world", "content": job.world_memory},
            *(
                {"scope": "channel", "key": key, "content": content}
                for key, content in job.channel_memories
            ),
            *(
                {"scope": "person", "key": key, "content": content}
                for key, content in job.people_memories
            ),
        ]
        return {
            "persona": job.persona,
            "rules": job.rules,
            "habitus": job.habitus,
            "current_mood": job.mood.to_dict(),
            "existing_digest": job.digest,
            "existing_open_items": job.open_items,
            "existing_memories": memories,
            "events": [event.to_log_dict() for event in job.events],
        }

    @staticmethod
    def _json_output(response: Any, operation: str) -> dict[str, Any]:
        if not response.output_text:
            raise RuntimeError(f"OpenAI {operation} response did not contain output text")
        return json.loads(response.output_text)

    @staticmethod
    def _emit_usage(
        response: Any, operation: str, started: float, prompt: PromptSpec, model: str
    ) -> None:
        usage = getattr(response, "usage", None)
        emit(
            "openai.response.completed",
            operation=operation,
            **prompt.log_fields(model),
            duration_ms=round((time.monotonic() - started) * 1000),
            response_id=getattr(response, "id", None),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        )
