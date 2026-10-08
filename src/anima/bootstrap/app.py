"""Composition root and command-line entry point."""

from __future__ import annotations

import logging
import asyncio
import json
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI

from anima.adapters.audio.mixer import DiscordAudioMixer
from anima.core.plugin_assembly import PluginAssembly
from anima.core.actor import PersonaActor
from anima.bootstrap.settings import Settings
from anima.core.context import ContextBuilder
from anima.adapters.discord.client import AnimaDiscordClient
from anima.adapters.discord.delivery import DiscordMessageSender
from anima.adapters.discord.voice import DiscordVoiceOutput, WavPCMAudio
from anima.adapters.discord.output import DiscordMessageOutput
from anima.core.events import SandboxEventMailbox
from anima.adapters.openai.memory_vector_store import MemoryVectorStore
from anima.core.memory import LocalMemoryRetriever
from anima.adapters.openai.client import OpenAIMemoryMaintainer, OpenAIResponder, OpenAISelfTimeDecider
from anima.core.state import FileStateStore
from anima.core.sandbox import require_separated_layout
from anima.core.sandbox_runtime import LimitedCalls, SandboxRouter, SandboxRuntime
from anima.core.expressions import FaceCatalog
from anima.core.access import ActivityModeStore, ActivityPolicy
from anima.core.proactivity import ProactiveDecisionEngine
from anima.adapters.openai.client import OpenAIAddressClassifier, OpenAIReactionClassifier
from anima.bootstrap.model_backends import OpenAIConversationFactory, decision_classifiers
from anima.bootstrap.cli import _pid_exists
from anima.bootstrap.process_guard import ProcessLock, mark_previous_unclean_shutdown
from anima.adapters.dashboard.server import DashboardServer
from anima.bootstrap.runtime_config import effective_runtime_config
from anima.capabilities.contracts import ToolRegistry
from anima.capabilities.responses import ResponseContributionRegistry
from anima.bootstrap.command_providers import ActivityCommandProvider, MaintenanceCommandProvider
from anima.capabilities.plugin_loader import PluginLoader
from anima.adapters.storage import FilePluginStorageFactory
from anima.core.services import SandboxServices
from anima.core.inventory import InventoryStore
from anima.core.jobs import PluginJobManager
from anima.core.modes import ModeRegistry
from anima.core.self_time import SelfTimeService
from anima.bootstrap.native_tools import WebSearchToolProvider
from anima.bootstrap.resource_providers import AttachmentResourceProvider, InventoryResourceProvider, MemoryResourceProvider, OpenItemsResourceProvider
from anima.core.resources import ResourceCollectionSpec, ResourceRegistration, ResourceRegistry, ResourceToolProvider


JST = ZoneInfo("Asia/Tokyo")


def build_client(settings: Settings, *, conversation_factory=None, decision_factory=None) -> AnimaDiscordClient:
    require_separated_layout(settings.state_root)
    sender = DiscordMessageSender(faces=FaceCatalog.load(settings.anima_root / "faces.json"))
    activity_modes = ActivityModeStore(settings.state_root)
    plugin_catalog = PluginLoader().load()
    unknown = settings.plugins - {manifest.name for manifest in plugin_catalog.manifests}
    if unknown:
        raise ValueError("unknown enabled plugins: " + ", ".join(sorted(unknown)))
    holder = {}
    api_limit = asyncio.Semaphore(4)
    router = SandboxRouter(settings.state_root,
        lambda key, root: build_sandbox(settings, key, root, sender, activity_modes,
                                        plugin_catalog, api_limit, lambda: holder["client"],
                                        conversation_factory=conversation_factory, decision_factory=decision_factory),
        policy=ActivityPolicy(settings.allowed_guild_ids, settings.dm_enabled))
    client = AnimaDiscordClient(actor=router, sender=sender, activity_modes=activity_modes,
        status_path=settings.state_root / "runtime" / "status.json",
        shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
        voice_enabled=any(manifest.uses_audio for manifest in plugin_catalog.manifests if manifest.name in settings.plugins),
        plugins=settings.plugins,
        persona_names=settings.persona_names, command_prefix=settings.command_prefix)
    holder["client"] = client
    return client


def build_sandbox(settings, key, root, sender, activity_modes, plugin_catalog, api_limit, client_provider,
                  *, conversation_factory=None, decision_factory=None):
    store = FileStateStore(root, sandbox_key=key, resource_root=settings.anima_root,
        recent_limit=settings.recent_limit, digest_max_lines=settings.digest_max_lines,
        memory_max_lines=settings.memory_max_lines, memory_strong_max=settings.memory_strong_max,
        log_retention_days=settings.log_retention_days, archive_retention_days=settings.archive_retention_days)
    clock = lambda: datetime.now(JST)
    api_client = lambda: AsyncOpenAI(api_key=settings.openai_api_key,
        timeout=settings.openai_response_timeout_seconds, max_retries=settings.openai_max_retries)
    memory_vector_store = MemoryVectorStore(root, AsyncOpenAI(api_key=settings.openai_api_key,
        timeout=settings.openai_maintenance_timeout_seconds, max_retries=settings.openai_max_retries), sandbox_key=str(key))
    inventory = InventoryStore(settings.state_root, key,
        max_items=settings.inventory_max_items, max_bytes=settings.inventory_max_bytes)
    inventory.cleanup_temporary(now=clock(), max_age=timedelta(hours=settings.inventory_temporary_retention_hours))
    jobs = PluginJobManager(root / "runtime" / "plugin-jobs.json")
    modes = ModeRegistry(root / "runtime" / "modes.json", [jobs])
    memory_retriever = LocalMemoryRetriever(root)
    conversation_factory = conversation_factory or OpenAIConversationFactory(
        responder_type=OpenAIResponder, maintainer_type=OpenAIMemoryMaintainer, self_time_type=OpenAISelfTimeDecider)
    address_classifier, classifier = decision_classifiers(settings,
        AsyncOpenAI(api_key=settings.openai_api_key, timeout=15, max_retries=0), decision_factory=decision_factory,
        store=store, semaphore=api_limit)
    decision_services = (classifier.backend,) if settings.decision_shadow_backend else ()
    responder = conversation_factory.responder(settings,
        tool_registry=ToolRegistry(), sandbox_key=key, memory_vector_store=memory_vector_store,
        memory_retriever=memory_retriever,
        appearance=(settings.anima_root / "appearance.md").read_text(encoding="utf-8"))
    actor = PersonaActor(store=store, context_builder=ContextBuilder(root / "attachments", inventory=inventory, modes=modes),
        responder=LimitedCalls(responder, api_limit),
        maintainer=LimitedCalls(conversation_factory.maintainer(settings), api_limit),
        memory_index=memory_vector_store, sender=sender, proactive_sender=sender, clock=clock,
        response_timeout_seconds=settings.openai_response_timeout_seconds,
        send_timeout_seconds=settings.discord_send_timeout_seconds,
        maintenance_timeout_seconds=settings.openai_maintenance_timeout_seconds,
        queue_size=settings.persona_queue_size, max_retries=settings.event_max_retries,
        retry_base_delay_seconds=settings.retry_base_delay_seconds,
        proactive_allowed=lambda: activity_modes.get(key) == "proactive",
        contextual_reply_allowed=lambda: activity_modes.get(key) != "silent",
        address_classifier=LimitedCalls(address_classifier, api_limit) if key.kind == "guild" else None)
    # A sender's optional face protocol must not implicitly enable an absent plugin.
    actor.face_preparer = None
    actor.face_presenter = None
    classifier = LimitedCalls(classifier, api_limit)
    if key.kind == "guild":
        actor.reactions = ProactiveDecisionEngine(store, classifier,
            decision_limit=settings.proactive_decision_daily_limit, daily_limit=settings.proactive_daily_limit,
            cooldown=settings.proactive_cooldown_seconds, bot_loop_window=settings.bot_loop_window_seconds,
            bot_loop_max_speaks=settings.bot_loop_max_speaks)
    inventory_provider = InventoryResourceProvider(inventory)
    assembly = PluginAssembly(actor, sender, settings.anima_root, inventory, jobs, api_client,
                              settings.openai_model, api_limit, client_provider, clock)
    assembly.publish("inventory_provider", inventory_provider)
    assembly.publish("state_root", settings.state_root)
    assembly.publish("speech_limiter", asyncio.Semaphore(1))
    assembly.publish("audio_mixer", DiscordAudioMixer())
    audio = DiscordVoiceOutput(client_provider(), key, WavPCMAudio, assembly.get("audio_mixer"))
    ingress = SandboxEventMailbox(key)
    ingress.bind(actor.submit)
    assembly.publish("reaction_classifier", classifier)
    assembly.publish("ambient_policy", {
        "proactive_daily_limit": settings.proactive_daily_limit,
        "proactive_cooldown_seconds": settings.proactive_cooldown_seconds,
        "bot_loop_window_seconds": settings.bot_loop_window_seconds,
        "bot_loop_max_speaks": settings.bot_loop_max_speaks,
    })
    administration_commands = (MaintenanceCommandProvider(actor, prefix=settings.command_prefix),
                               ActivityCommandProvider(activity_modes, actor, prefix=settings.command_prefix))
    assembly.publish("web_search_provider", WebSearchToolProvider(enabled=settings.enable_web_search))
    assembly.publish("web_search_enabled", settings.enable_web_search)
    configuration = settings.plugin_configuration
    plugins = plugin_catalog.create(settings.plugins, SandboxServices(key, {
        "assembly": assembly, "jobs": jobs, "inventory": inventory, "background_notifier": sender,
        "audio_output": audio, "event_ingress": ingress,
        "message_output": DiscordMessageOutput(client_provider(), key),
        "memory_retriever": memory_retriever,
        "plugin_storage_factory": FilePluginStorageFactory(settings.state_root, key),
        "plugin_status_path": root / "runtime" / "plugins.json"}), configuration)
    registrations = (
        ResourceRegistration(ResourceCollectionSpec("core.inventory", "core", "継続して残している持ち物", "sandbox", frozenset({"list", "read", "write", "delete", "export", "import"}), writable_content="text", transfer_policy="copy"), inventory_provider),
        ResourceRegistration(ResourceCollectionSpec("core.temporary_artifacts", "core", "今回生成した一時成果物", "sandbox", frozenset({"list", "read", "delete", "export"}), transfer_policy="consume"), inventory_provider),
        ResourceRegistration(ResourceCollectionSpec("interface.current_attachments", "core", "現在の発言に添付されたファイル", "turn", frozenset({"list", "read", "export"}), transfer_policy="copy"), AttachmentResourceProvider(inventory)),
        ResourceRegistration(ResourceCollectionSpec("core.memory", "core", "このSandboxの長期記憶", "sandbox", frozenset({"search", "read"}), max_search_results=5), MemoryResourceProvider(root, memory_retriever)),
        ResourceRegistration(ResourceCollectionSpec("core.open_items", "core", "未完了事項。self timeでは全文更新も可能", "sandbox", frozenset({"list", "read", "write"}), writable_content="text"), OpenItemsResourceProvider(root)))
    resources = ResourceRegistry((*registrations, *plugins.resources))
    primitive_resources = ResourceToolProvider(resources)
    plugin_tools = plugins.tools
    responder.tool_registry = ToolRegistry((*plugin_tools.providers, primitive_resources), owners=plugin_tools.owners)
    responder.response_registry = ResponseContributionRegistry(plugins.responses.providers)
    internal_providers, internal_owners, internal_actions = [], {}, set()
    for plugin in plugins.instances:
        for provider in getattr(plugin, "internal_tool_providers", plugin.tool_providers):
            internal_providers.append(provider)
            internal_owners[id(provider)] = plugin.manifest.name
        internal_actions.update(getattr(plugin, "internal_action_tools", ()))
    internal_tools = ToolRegistry((*internal_providers, primitive_resources), owners=internal_owners)
    self_time = SelfTimeService(root / "runtime" / "self-time.json",
        LimitedCalls(conversation_factory.self_time(settings, client=api_client(),
            tool_registry=internal_tools, sandbox_key=key, allowed_action_tools=frozenset(internal_actions)), api_limit),
        lambda: {**store.self_time_context(), "inventory": inventory.summary(), "active_modes": modes.summary()},
        lambda decision, now: store.commit_self_time(decision, now=now), clock=clock,
        allowed=lambda: activity_modes.get(key) == "proactive", busy=lambda: jobs.active_count() > 0,
        interval_seconds=settings.self_time_interval_seconds, daily_limit=settings.self_time_daily_limit,
        max_iterations=settings.self_time_max_iterations)
    actor.self_time = self_time
    mode_providers = tuple(provider for plugin in plugins.instances for provider in getattr(plugin, "mode_providers", ()))
    modes.replace_providers((jobs, *mode_providers, self_time))
    self_time.modes_changed = modes.refresh
    modes.refresh()
    path = root / "runtime" / "resources.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"collections": resources.snapshot()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def reload_configuration():
        responder.reload_configuration(appearance=(settings.anima_root / "appearance.md").read_text(encoding="utf-8"))
        for plugin in plugins.instances:
            callback = getattr(plugin, "reload_configuration", None)
            if callback is not None:
                callback()
    from anima.capabilities.commands import CommandRegistry
    runtime = SandboxRuntime(actor=actor, commands=CommandRegistry((*plugins.commands.providers, *administration_commands)),
                             plugins=plugins, jobs=jobs,
                             modes=modes, self_time=self_time, reload_configuration=reload_configuration,
                             audio_output=audio, services=(audio, ingress, *decision_services))
    for name in ("voice", "music", "dj", "reminders"):
        setattr(runtime, name, assembly.get(name))
    return runtime



def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = settings.state_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    telemetry = logging.getLogger("anima.telemetry")
    from anima.core.plugin_logs import LOGGER as plugin_logger, PluginLogHandler
    plugin_logger.setLevel(logging.INFO)
    plugin_handler = PluginLogHandler(settings.state_root, settings.operational_log_retention_days, settings.plugins)
    plugin_logger.addHandler(plugin_handler)
    telemetry.addHandler(plugin_handler)
    handler = TimedRotatingFileHandler(
        runtime / "anima.jsonl",
        when="midnight",
        backupCount=settings.operational_log_retention_days,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    telemetry.addHandler(handler)
    application = TimedRotatingFileHandler(
        runtime / "bot.log",
        when="midnight",
        backupCount=settings.operational_log_retention_days,
        encoding="utf-8",
    )
    application.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(application)
    errors = TimedRotatingFileHandler(
        runtime / "error.log",
        when="midnight",
        backupCount=settings.operational_log_retention_days,
        encoding="utf-8",
    )
    errors.setLevel(logging.ERROR)
    errors.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(errors)


def main() -> None:
    settings = Settings.load()
    runtime = settings.state_root / "runtime"
    with ProcessLock(runtime / "bot.lock"):
        configure_logging(settings)
        if mark_previous_unclean_shutdown(
            runtime / "status.json", now=datetime.now(JST), pid_exists=_pid_exists
        ):
            logging.getLogger(__name__).warning("Previous Bot process ended without a clean shutdown")
        client = build_client(settings)
        with DashboardServer(
            settings.anima_root,
            state_root=settings.state_root,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            policy=ActivityPolicy(settings.allowed_guild_ids, settings.dm_enabled),
            admin_token=settings.dashboard_admin_token,
            effective_config=effective_runtime_config(settings),
            reload_configuration=client.actor.reload_configuration,
        ):
            client.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
