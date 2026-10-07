import unittest

from anima.bootstrap.runtime import RuntimeHost
from anima.capabilities.plugins import PluginCatalog
from anima.core.sandbox import SandboxKey
from anima.core.services import SandboxServices
from anima.plugins.echo import EchoPluginDefinition


class ServiceTests(unittest.TestCase):
    def test_require(self):
        services = SandboxServices(SandboxKey("test", "1"), {"name": "value"})
        self.assertEqual(services.require("name", str), "value")
        with self.assertRaises(LookupError):
            services.require("missing", str)
        with self.assertRaises(TypeError):
            services.require("name", int)


class Service:
    def __init__(self, log, *, fail=False):
        self.log = log
        self.fail = fail

    async def start(self):
        self.log.append("service:start")
        if self.fail:
            raise RuntimeError("service failed")

    async def stop(self):
        self.log.append("service:stop")


class RuntimeHostTests(unittest.IsolatedAsyncioTestCase):
    def host(self, factory=None):
        return RuntimeHost(
            PluginCatalog((EchoPluginDefinition(),)), frozenset({"echo"}),
            factory or (lambda key: SandboxServices(key)),
            {"echo": {"prefix": "> "}},
        )

    async def test_sandbox_lifecycle_and_snapshot(self):
        host = self.host()
        one = SandboxKey("discord_guild", "1")
        two = SandboxKey("discord_guild", "2")
        runtime = await host.start_sandbox(one)
        self.assertIs(await host.start_sandbox(one), runtime)
        self.assertIs(host.sandbox(one), runtime)
        self.assertEqual(runtime.snapshot()["sandbox"], "guild:1")
        self.assertEqual(len(runtime.tools.providers), 1)
        self.assertEqual(len(runtime.commands.specs), 1)
        await host.start_sandbox(two)
        self.assertEqual(len(host.snapshots()), 2)
        await host.stop_sandbox(one)
        self.assertIsNone(host.sandbox(one))
        await host.stop_sandbox(one)
        await host.stop()
        self.assertEqual(host.snapshots(), ())

    async def test_rejects_cross_sandbox_services(self):
        host = self.host(lambda key: SandboxServices(SandboxKey("test", "wrong")))
        with self.assertRaises(ValueError):
            await host.start_sandbox(SandboxKey("test", "right"))

    async def test_service_lifecycle_is_deduplicated_and_wraps_plugins(self):
        log = []
        service = Service(log)
        host = self.host(lambda key: SandboxServices(key, {"one": service, "two": service}))
        key = SandboxKey("test", "service")
        await host.start_sandbox(key)
        await host.stop()
        self.assertEqual(log, ["service:start", "service:stop"])

    async def test_service_start_failure_rolls_back_started_services(self):
        log = []
        first = Service(log)
        failing = Service(log, fail=True)
        host = self.host(lambda key: SandboxServices(key, {"first": first, "fail": failing}))
        with self.assertRaisesRegex(RuntimeError, "service failed"):
            await host.start_sandbox(SandboxKey("test", "failure"))
        self.assertEqual(log, ["service:start", "service:start", "service:stop"])


if __name__ == "__main__":
    unittest.main()
