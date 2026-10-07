"""Own one independent actor and playback pipeline per sandbox."""

import asyncio
from dataclasses import dataclass

from anima.core.sandbox import SandboxKey, list_sandboxes, require_separated_layout
from anima.core.telemetry import sandbox_context
from anima.core.access import ActivityPolicy


@dataclass
class SandboxRuntime:
    actor: object
    voice: object = None
    music: object = None
    dj: object = None
    reminders: object = None
    commands: object = None
    plugins: object = None
    reload_configuration: object = None
    jobs: object = None
    modes: object = None
    self_time: object = None
    services: tuple = ()
    started_services: tuple = ()
    audio_output: object = None


class SandboxRouter:
    def __init__(self, root, factory, *, max_sandboxes=100, policy=None):
        self.policy = policy or ActivityPolicy()
        self.root = root
        self.factory = factory
        self.max_sandboxes = max_sandboxes
        self.runtimes = {}
        self.client = None
        self._lock = asyncio.Lock()
        self._started = False

    def bind(self, client):
        self.client = client

    async def start(self):
        require_separated_layout(self.root)
        self._started = True
        try:
            for key in list_sandboxes(self.root):
                if self.policy.allows(key):
                    await self.runtime(key)
        except BaseException:
            await self.stop()
            raise

    async def runtime(self, key):
        if not self.policy.allows(key):
            raise PermissionError("sandbox activity is disabled")
        if not self._started:
            raise RuntimeError("sandbox router is not started")
        async with self._lock:
            if key not in self.runtimes:
                if len(self.runtimes) >= self.max_sandboxes:
                    raise RuntimeError("sandbox capacity exceeded")
                token = sandbox_context.set(str(key))
                try:
                    runtime = self.factory(key, key.path(self.root))
                    try:
                        for service in runtime.services:
                            if any(service is started for started in runtime.started_services):
                                continue
                            await service.start()
                            runtime.started_services += (service,)
                        if runtime.jobs is not None:
                            await runtime.jobs.start()
                        if runtime.self_time is not None:
                            await runtime.self_time.start()
                        if runtime.plugins is not None:
                            await runtime.plugins.start()
                        await runtime.actor.start()
                        if runtime.plugins is None and runtime.reminders is not None:
                            await runtime.reminders.start()
                        if runtime.plugins is None and runtime.voice is not None:
                            runtime.voice.bind(self.client)
                            await runtime.voice.start()
                        if runtime.plugins is None and runtime.music is not None:
                            runtime.music.bind(self.client)
                    except BaseException:
                        await self._stop_runtime(runtime)
                        raise
                    self.runtimes[key] = runtime
                finally:
                    sandbox_context.reset(token)
            return self.runtimes[key]

    async def submit(self, event, *, allow_reactions=True):
        key = SandboxKey.for_event(event)
        runtime = await self.runtime(key)
        token = sandbox_context.set(str(key))
        try:
            if allow_reactions:
                return await runtime.actor.submit(event)
            return await runtime.actor.submit(event, allow_reactions=False)
        finally:
            sandbox_context.reset(token)

    async def mark_deleted(self, key, channel_id, event_id):
        runtime = await self.runtime(key)
        token = sandbox_context.set(str(key))
        try:
            return await runtime.actor.mark_deleted(channel_id, event_id)
        finally:
            sandbox_context.reset(token)

    def pending_events(self):
        return tuple(event for runtime in self.runtimes.values() for event in runtime.actor.pending_events())

    def reload_configuration(self):
        """Reload fixed configuration for every active sandbox runtime."""
        count = 0
        for runtime in tuple(self.runtimes.values()):
            if runtime.reload_configuration is not None:
                runtime.reload_configuration()
                count += 1
        return count

    def abandon(self, event):
        self.runtimes[SandboxKey.for_event(event)].actor.abandon(event)

    async def stop(self):
        self._started = False
        try:
            await asyncio.gather(*(self._stop_runtime(runtime) for runtime in self.runtimes.values()))
        finally:
            self.runtimes.clear()

    @staticmethod
    async def _stop_runtime(runtime):
        failures = []
        try:
            await SandboxRouter._stop_components(runtime)
        except Exception as error:
            failures.append(error)
        finally:
            services, runtime.started_services = runtime.started_services, ()
            for service in reversed(services):
                try:
                    await service.stop()
                except Exception as error:
                    failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("sandbox shutdown failed", failures)

    @staticmethod
    async def _stop_components(runtime):
        stops = []
        if runtime.plugins is not None:
            stops.extend(((runtime.actor, ()), (runtime.plugins, ())))
        else:
            if runtime.reminders is not None:
                stops.append((runtime.reminders, ()))
            stops.append((runtime.actor, ()))
            stops.extend((target, ("bot_stopping",)) for target in (runtime.dj, runtime.music) if target is not None)
        stops.extend((target, ()) for target in (runtime.self_time, runtime.jobs) if target is not None)
        if runtime.plugins is None and runtime.voice is not None:
            stops.append((runtime.voice, ()))
        failures = []
        for target, arguments in stops:
            try:
                await target.stop(*arguments)
            except Exception as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("sandbox components shutdown failed", failures)


class LimitedCalls:
    """Share an API concurrency limit without sharing conversation state."""

    def __init__(self, target, semaphore):
        self.target = target
        self.semaphore = semaphore

    def __getattr__(self, name):
        method = getattr(self.target, name)

        async def call(*args, **kwargs):
            async with self.semaphore:
                return await method(*args, **kwargs)

        return call
