"""Transport-neutral command contracts and sandbox-scoped registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal, Protocol, runtime_checkable

from anima.capabilities.contracts import ActionRecord, ContextReference, PermissionSet
from anima.core.models import Event
from anima.core.sandbox import SandboxKey
from anima.capabilities.availability import (
    Availability,
    AvailabilityProvider,
    AvailabilityStatus,
)


SAFE_COMMAND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class CommandChoice:
    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 100 or not self.value or len(self.value) > 100:
            raise ValueError("command choice is invalid")


@dataclass(frozen=True, slots=True)
class CommandParameter:
    name: str
    description: str
    kind: Literal["string", "integer", "boolean", "choice"]
    required: bool = True
    choices: tuple[CommandChoice, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        _name(self.name)
        _description(self.description)
        if self.kind not in {"string", "integer", "boolean", "choice"}:
            raise ValueError("command parameter kind is invalid")
        if self.kind == "choice" and not self.choices:
            raise ValueError("choice parameter requires choices")
        if self.kind != "choice" and self.choices:
            raise ValueError("choices require choice parameter")
        if (self.minimum is not None or self.maximum is not None) and self.kind not in {
            "string", "integer",
        }:
            raise ValueError("parameter bounds are unsupported")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter minimum exceeds maximum")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    path: tuple[str, ...]
    description: str
    parameters: tuple[CommandParameter, ...] = ()
    permission: str | None = None
    guild_only: bool = False
    required_namespace: str | None = None
    response_mode: Literal["immediate", "deferred"] = "immediate"

    def __post_init__(self) -> None:
        if not 1 <= len(self.path) <= 2:
            raise ValueError("command path must have one or two segments")
        for segment in self.path:
            _name(segment)
        _description(self.description)
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("command parameter names must be unique")
        if any(not parameter.required for parameter in self.parameters[:-1]) and any(
            parameter.required for parameter in self.parameters[1:]
        ):
            raise ValueError("required parameters must precede optional parameters")
        if self.permission is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.permission):
            raise ValueError("command permission is invalid")
        if self.response_mode not in {"immediate", "deferred"}:
            raise ValueError("command response mode is invalid")
        if self.required_namespace is not None and not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", self.required_namespace
        ):
            raise ValueError("command namespace is invalid")


@dataclass(frozen=True, slots=True)
class CommandContext:
    source: Event
    sandbox_key: SandboxKey
    actor_id: str
    permissions: PermissionSet = PermissionSet()

    def __post_init__(self) -> None:
        if self.source.sandbox_key is not None and self.source.sandbox_key != str(self.sandbox_key):
            raise ValueError("command context sandbox does not match source event")


@dataclass(frozen=True, slots=True)
class CommandResult:
    text: str
    visibility: Literal["public", "private"] = "public"
    attachments: tuple[Path, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    references: tuple[ContextReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip() or len(self.text) > 2_000:
            raise ValueError("command result text is invalid")
        if self.visibility not in {"public", "private"}:
            raise ValueError("command result visibility is invalid")


@runtime_checkable
class CommandProvider(Protocol):
    def commands(self) -> tuple[CommandSpec, ...]: ...

    async def execute_command(
        self, path: tuple[str, ...], arguments: Mapping[str, object], context: CommandContext
    ) -> CommandResult: ...


class CommandRegistry:
    def __init__(self, providers: Sequence[CommandProvider] = ()) -> None:
        self.providers = tuple(providers)
        if any(not isinstance(provider, CommandProvider) for provider in self.providers):
            raise TypeError("command providers must implement CommandProvider")
        routes = {}
        for provider in self.providers:
            specs = provider.commands()
            if not isinstance(specs, tuple):
                raise ValueError("command provider must return a tuple")
            for spec in specs:
                if not isinstance(spec, CommandSpec):
                    raise ValueError("provider returned an invalid command spec")
                if spec.path in routes:
                    raise ValueError("duplicate command path: " + " ".join(spec.path))
                routes[spec.path] = (spec, provider)
        self._routes = routes

    @property
    def specs(self) -> tuple[CommandSpec, ...]:
        return tuple(spec for spec, _provider in self._routes.values())

    def spec(self, path: tuple[str, ...]) -> CommandSpec | None:
        route = self._routes.get(path)
        return route[0] if route else None

    async def execute(
        self, path: tuple[str, ...], arguments: Mapping[str, object], context: CommandContext
    ) -> CommandResult:
        route = self._routes.get(path)
        if route is None:
            return CommandResult("不明なコマンドです。", "private")
        spec, provider = route
        if spec.guild_only and context.sandbox_key.kind != "guild":
            return CommandResult("このコマンドはサーバー内で使ってね。", "private")
        if (
            spec.required_namespace is not None
            and context.sandbox_key.namespace != spec.required_namespace
        ):
            return CommandResult(
                f"このコマンドは{spec.required_namespace}では使えません。", "private"
            )
        if spec.permission and not context.permissions.allows(spec.permission):
            return CommandResult("このコマンドを実行する権限がありません。", "private")
        if isinstance(provider, AvailabilityProvider):
            status = await provider.capability_availability(
                "command", " ".join(path), context
            )
            if not isinstance(status, AvailabilityStatus):
                raise TypeError("availability provider must return AvailabilityStatus")
            if status.state is not Availability.AVAILABLE:
                return CommandResult(
                    status.reason or f"このコマンドは{status.state.value}です。",
                    "private",
                )
        if not _arguments_match(spec.parameters, arguments):
            return CommandResult("コマンドの引数が不正です。", "private")
        result = await provider.execute_command(path, arguments, context)
        if not isinstance(result, CommandResult):
            raise TypeError("command provider must return CommandResult")
        if spec.permission and result.visibility != "private":
            return CommandResult(
                result.text, "private", result.attachments, result.actions, result.references
            )
        return result


def _arguments_match(parameters: tuple[CommandParameter, ...], arguments: Mapping[str, object]) -> bool:
    expected = {parameter.name: parameter for parameter in parameters}
    if set(arguments) - set(expected):
        return False
    for parameter in parameters:
        if parameter.name not in arguments:
            if parameter.required:
                return False
            continue
        value = arguments[parameter.name]
        if parameter.kind in {"string", "choice"}:
            if not isinstance(value, str):
                return False
            size = len(value)
        elif parameter.kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return False
            size = value
        elif not isinstance(value, bool):
            return False
        else:
            continue
        if parameter.kind == "choice" and value not in {choice.value for choice in parameter.choices}:
            return False
        if parameter.minimum is not None and size < parameter.minimum:
            return False
        if parameter.maximum is not None and size > parameter.maximum:
            return False
    return True


def _name(value: str) -> None:
    if not SAFE_COMMAND.fullmatch(value):
        raise ValueError("command name is invalid")


def _description(value: str) -> None:
    if not value.strip() or len(value) > 100:
        raise ValueError("command description is invalid")
