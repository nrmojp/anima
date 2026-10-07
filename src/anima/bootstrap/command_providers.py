"""Command providers for built-in capabilities."""

from __future__ import annotations


from datetime import datetime

from zoneinfo import ZoneInfo

from anima.core.access import ACTIVITY_MODES

from anima.capabilities.contracts import ActionRecord

from anima.capabilities.commands import CommandChoice, CommandParameter, CommandResult, CommandSpec

from anima.core.models import Event


JST = ZoneInfo("Asia/Tokyo")

def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None

class MaintenanceCommandProvider:
    def __init__(self, actor, *, prefix="anima") -> None:
        self.actor = actor
        self.prefix = prefix

    def commands(self) -> tuple[CommandSpec, ...]:
        return (CommandSpec(
            (self.prefix + "-maintenance",), "実験用の内部処理を今すぐ実行します",
            (CommandParameter("kind", "実行する処理", "choice", choices=(
                CommandChoice("昼寝（あらすじ更新）", "nap"),
                CommandChoice("睡眠（記憶定着）", "sleep"),
                CommandChoice("振り返り（習性更新）", "reflection"),
                CommandChoice("Self Time（自律活動）", "self_time"),
            )),),
            permission="manage_sandbox", guild_only=True, response_mode="deferred",
        ),)

    async def execute_command(self, path, arguments, context) -> CommandResult:
        kind = str(arguments["kind"])
        if kind == "self_time":
            decisions = await self.actor.force_self_time()
            action_labels = {"none": "何もしない", "continue": "継続", "finish": "完了"}
            action = action_labels[decisions[-1].action]
            return CommandResult(
                f"Self Timeを{len(decisions)}反復実行しました（最終判断: {action}）。",
                "private",
            )
        operation = await self.actor.force_maintenance(kind)
        labels = {"nap": "昼寝", "sleep": "睡眠", "reflection": "振り返り"}
        return CommandResult(f"{labels[operation]}を完了しました。", "private")

class ActivityCommandProvider:
    def __init__(self, modes, actor, *, clock=lambda: datetime.now(JST), prefix="anima") -> None:
        self.modes = modes
        self.actor = actor
        self.clock = clock
        self.prefix = prefix

    def commands(self) -> tuple[CommandSpec, ...]:
        return (CommandSpec(
            (self.prefix + "-mode",), "ペルソナの活動レベルを変更・確認します",
            (CommandParameter("mode", "活動レベル", "choice", choices=tuple(
                CommandChoice(label, value) for value, label in (
                    ("silent", "完全に沈黙"), ("reply", "呼ばれたら応答"),
                    ("react", "応答＋リアクション"),
                    ("proactive", "応答＋リアクション＋自発発言"),
                    ("status", "現在の設定を確認"),
                )
            )),),
            permission="manage_sandbox", guild_only=True,
        ),)

    async def execute_command(self, path, arguments, context) -> CommandResult:
        mode = str(arguments["mode"])
        if mode == "status":
            return CommandResult(_activity_message(self.modes.details(context.sandbox_key)), "private")
        previous = self.modes.get(context.sandbox_key)
        details = self.modes.set(
            context.sandbox_key, mode, changed_by=context.actor_id, changed_at=self.clock(),
        )
        if ACTIVITY_MODES.index(mode) < ACTIVITY_MODES.index(previous):
            await self.actor.cancel_pending_reactions()
        return CommandResult(
            _activity_message(details), "private",
            actions=(ActionRecord("activity", "set_mode", f"活動レベルを{mode}へ変更した"),),
        )

def _activity_message(details) -> str:
    labels = {
        "silent": "完全に沈黙", "reply": "呼ばれたら応答",
        "react": "応答＋リアクション", "proactive": "応答＋リアクション＋自発発言",
    }
    return f"現在の活動レベル: **{labels[details['mode']]}** (`{details['mode']}`)"

def command_event(result: CommandResult, source: Event) -> Event | None:
    if not result.actions and not result.references:
        return None
    return Event(
        id=f"command-{source.id}", ts=datetime.now(JST), kind=source.kind,
        channel_id=source.channel_id, channel_name=source.channel_name,
        author_id="self", author_name="自分", text=result.text,
        guild_id=source.guild_id, sandbox_key=source.sandbox_key,
        actions=result.actions, references=result.references,
        acts=tuple(action.action for action in result.actions),
    )
