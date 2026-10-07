from __future__ import annotations

import unittest

import discord
from discord import app_commands

from anima.adapters.discord.commands import PluginCommandRegistrar
from anima.capabilities.commands import (
    CommandChoice, CommandParameter, CommandSpec,
)


class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)


class PluginCommandRegistrarTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.guild = discord.Object(id=1)
        self.registrar = PluginCommandRegistrar(self.client)

    def test_installs_direct_and_grouped_specs(self):
        specs = (
            CommandSpec(
                ("echo",), "Echo text",
                (CommandParameter("text", "Text", "string"),),
                guild_only=True,
            ),
            CommandSpec(
                ("sample", "set"), "Set value",
                (
                    CommandParameter(
                        "mode", "Mode", "choice",
                        choices=(CommandChoice("On", "on"),),
                    ),
                    CommandParameter(
                        "count", "Count", "integer", required=False,
                        minimum=0, maximum=10,
                    ),
                ),
                permission="manage_sandbox",
            ),
        )
        self.assertEqual(self.registrar.install(specs, guild=self.guild), 2)
        self.assertEqual(
            [command.name for command in self.client.tree.get_commands(guild=self.guild)],
            ["echo", "sample"],
        )

    def test_skips_existing_root_and_rejects_mixed_root(self):
        async def existing(interaction: discord.Interaction) -> None:
            del interaction

        self.client.tree.add_command(app_commands.Command(
            name="echo", description="Existing", callback=existing,
        ), guild=self.guild)
        self.assertEqual(self.registrar.install((
            CommandSpec(("echo",), "New"),
        ), guild=self.guild), 0)
        with self.assertRaises(ValueError):
            self.registrar.install((
                CommandSpec(("mixed",), "Direct"),
                CommandSpec(("mixed", "child"), "Child"),
            ), guild=self.guild)


if __name__ == "__main__":
    unittest.main()
