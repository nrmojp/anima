"""Publish transport-neutral Plugin commands through Discord."""

from __future__ import annotations

import inspect
import discord
from discord import app_commands

from anima.adapters.discord.command_execution import execute_registered_command
from anima.capabilities.commands import CommandSpec
from anima.core.sandbox import SandboxKey


class PluginCommandRegistrar:
    def __init__(self, client) -> None:
        self.client = client

    def install(self, specs: tuple[CommandSpec, ...], *, guild: discord.Object) -> int:
        roots = {spec.path[0] for spec in specs
                 if self.client.tree.get_command(spec.path[0], guild=guild) is None}
        selected = tuple(spec for spec in specs if spec.path[0] in roots)
        direct = {spec.path[0] for spec in selected if len(spec.path) == 1}
        grouped = {spec.path[0] for spec in selected if len(spec.path) == 2}
        conflict = direct & grouped
        if conflict:
            raise ValueError("command path is both a command and a group: " + min(conflict))
        groups = {}
        for spec in selected:
            command = self._command(spec)
            if len(spec.path) == 1:
                self.client.tree.add_command(command, guild=guild)
            else:
                root = spec.path[0]
                group = groups.get(root)
                if group is None:
                    group = app_commands.Group(name=root, description=f"{root} commands.")
                    groups[root] = group
                    self.client.tree.add_command(group, guild=guild)
                group.add_command(command)
        return len(selected)

    def _command(self, spec: CommandSpec):
        async def callback(interaction: discord.Interaction, **arguments) -> None:
            runtime = await self.client.actor.runtime(
                SandboxKey("guild", str(interaction.guild.id))
            )
            await execute_registered_command(
                runtime, interaction, spec.path,
                {name: getattr(value, "value", value)
                 for name, value in arguments.items() if value is not None},
                "/" + " ".join(spec.path),
            )

        parameters = [inspect.Parameter(
            "interaction", inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=discord.Interaction,
        )]
        descriptions, choices = {}, {}
        for parameter in spec.parameters:
            annotation = {"string": str, "choice": str, "integer": int, "boolean": bool}[
                parameter.kind
            ]
            if parameter.kind == "integer" and (
                parameter.minimum is not None or parameter.maximum is not None
            ):
                annotation = app_commands.Range[
                    int,
                    parameter.minimum if parameter.minimum is not None else -2**53,
                    parameter.maximum if parameter.maximum is not None else 2**53,
                ]
            parameters.append(inspect.Parameter(
                parameter.name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation,
                default=inspect.Parameter.empty if parameter.required else None,
            ))
            descriptions[parameter.name] = parameter.description
            if parameter.kind == "choice":
                choices[parameter.name] = [
                    app_commands.Choice(name=item.name, value=item.value)
                    for item in parameter.choices
                ]
        callback.__signature__ = inspect.Signature(parameters)
        if descriptions:
            callback = app_commands.describe(**descriptions)(callback)
        if choices:
            callback = app_commands.choices(**choices)(callback)
        if spec.permission:
            callback = app_commands.default_permissions(manage_guild=True)(callback)
        if spec.guild_only or spec.required_namespace == "discord_guild":
            callback = app_commands.guild_only()(callback)
        return app_commands.Command(
            name=spec.path[-1], description=spec.description, callback=callback,
        )
