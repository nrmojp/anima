"""Small reference plugin demonstrating tools, commands, config, and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from anima.capabilities.commands import (
    CommandContext, CommandParameter, CommandResult, CommandSpec,
)
from anima.capabilities.configuration import ConfigField
from anima.capabilities.plugins import PluginManifest
from anima.capabilities.tools import CapabilityContext, FunctionToolSpec, ToolResult
from anima.core.services import SandboxServices


_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class EchoPluginDefinition:
    manifest: PluginManifest = PluginManifest(
        name="echo",
        version="1.0.0",
        default_enabled=True,
        provides=("echo",),
        description="Reference capability that echoes text without external services.",
        configuration=(ConfigField(
            key="prefix", env="ANIMA_ECHO_PREFIX", attribute="prefix",
            label="Message prefix", category="Echo", kind="text",
            default="Echo: ", plugin="echo",
        ),),
    )

    def create(
        self, sandbox_services: SandboxServices, configuration: Mapping[str, object]
    ) -> "EchoPlugin":
        del sandbox_services
        return EchoPlugin(self.manifest, str(configuration["prefix"]))


class EchoPlugin:
    resource_registrations = ()
    def __init__(self, manifest: PluginManifest, prefix: str) -> None:
        self.manifest = manifest
        self.prefix = prefix
        self.tool_providers = (self,)
        self.command_providers = (self,)
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    def snapshot(self) -> Mapping[str, object]:
        return {"name": self.manifest.name, "running": self.running}

    async def tools(self, context: CapabilityContext) -> tuple[FunctionToolSpec, ...]:
        del context
        return (FunctionToolSpec("echo", "Echo a message.", _MESSAGE_SCHEMA),)

    async def execute_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        context: CapabilityContext,
        invocation_id: str,
    ) -> ToolResult:
        del context, invocation_id
        if name != "echo":
            return ToolResult("rejected", {"ok": False, "error": "unknown_tool"})
        return ToolResult("success", {"ok": True, "text": self.prefix + str(arguments["message"])})

    def commands(self) -> tuple[CommandSpec, ...]:
        return (CommandSpec(
            ("echo",), "Echo a message.",
            (CommandParameter("message", "Message to echo.", "string"),),
        ),)

    async def execute_command(
        self, path: tuple[str, ...], arguments: Mapping[str, object], context: CommandContext
    ) -> CommandResult:
        del path, context
        return CommandResult(self.prefix + str(arguments.get("message", "")))


PLUGIN = EchoPluginDefinition()
