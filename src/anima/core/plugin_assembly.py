"""Sandbox-local composition ports, without knowledge of concrete features."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class PluginAssembly:
    """Trusted plugins bind optional actor routes during synchronous construction.

    Secrets are not exposed here. External clients are obtained from an explicit
    factory; all durable access is restricted to the already bound sandbox.
    Construction must finish before the actor or plugin jobs are started.
    """

    actor: object
    sender: object
    assets: Path
    inventory: object
    jobs: object
    api_client: Callable
    model: str
    limiter: object
    client_provider: Callable
    clock: Callable
    values: dict = field(default_factory=dict)

    def publish(self, name: str, value: object) -> None:
        if not name or name in self.values:
            raise ValueError("duplicate or empty plugin assembly binding")
        self.values[name] = value

    def get(self, name: str, default=None):
        return self.values.get(name, default)
