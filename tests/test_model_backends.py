import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from pathlib import Path
from tempfile import TemporaryDirectory
import httpx
from openai import APITimeoutError, APIConnectionError, APIStatusError

from anima.core.model_contracts import DecisionQuestion as Q, DecisionRequest as R, DecisionAnswer as A, DecisionResult as Result, ConversationRequest, ConversationResult
from anima.core.decision_policy import DecisionPolicy as Policy
from anima.core.decision_requests import AddressClassifier, EngagementClassifier
from anima.adapters.openai.decisions import OpenAIDecisionBackend
from anima.adapters.openai.response_decisions import OpenAIResponseDecisionBackend
from anima.adapters.openai.conversation import OpenAIConversationBackend, OpenAIConversationRequest
from anima.core.external_errors import TransientExternalError
from anima.bootstrap.settings import Settings, ConfigurationError
from anima.bootstrap.model_backends import decision_classifiers


class ContractTests(unittest.TestCase):
    def test_questions_requests(self):
        for args in [("", ""), ("q", "", "bad"), ("q", "", "choice"),
                     ("q", "", "choice", ("a", "a")), ("q", "", "score", ("", "b")),
                     ("q", "", "predicate", ("a",))]:
            with self.assertRaises(ValueError): Q(*args)
        for qs in [(), (Q("q", ""), Q("q", ""))]:
            with self.assertRaises(ValueError): R("", qs)

    def test_validation(self):
        q = Q("q", "")
        request = R("", (q,))
        for tokens in [True,-1,1.5]:
            with self.assertRaises(ValueError): Result((A("q","predicate",True),),input_tokens=tokens).validate(request)
        for a in [A("x", "predicate", True), A("q", "choice", True), A("q", "predicate", 1),
                  A("q", "predicate", True, status="bad"), A("q", "predicate", True, status="refused"),
                  A("q", "predicate", True, probability=float("nan")),
                  A("q", "predicate", True, confidence=2), A("q", "predicate", True, probability=True),
                  A("q", "predicate", True, probabilities=(("a", 1),))]:
            with self.assertRaises(ValueError): a.validate(q)
        for answers in [(), (A("q", "predicate", True), A("q", "predicate", True)), (A("x", "predicate", True),)]:
            with self.assertRaises(ValueError): Result(answers).validate(request)
        A("q", "predicate", status="refused").validate(q)
        A("q", "predicate", status="unavailable").validate(q)
        self.assertTrue(Result((A("q", "predicate", True),)).validate(request)["q"].value)
        q = Q("q", "", "choice", ("a", "b"))
        for a in [A("q", "choice", "x"), A("q", "choice", "a", probability=.2),
                  A("q", "choice", "a", probabilities=(("a", .3),)),
                  A("q", "choice", "a", probabilities=(("a", .3), ("b", .3))),
                  A("q", "choice", "a", probabilities=(("a", 1), ("a", 0)))]:
            with self.assertRaises(ValueError): a.validate(q)
        A("q", "choice", "a", probabilities=(("a", .8), ("b", .2))).validate(q)
        score = Q("q", "", "score", ("low", "high"))
        A("q", "score", .5, probabilities=(("low", .5), ("high", .5))).validate(score)
        for v in [True, "low", -1, 2, float("nan")]:
            with self.assertRaises(ValueError): A("q", "score", v).validate(score)

    def test_policy(self):
        for args in [(float("nan"),), (-1,), (True,), (None, 2)]:
            with self.assertRaises(ValueError): Policy(*args)
        self.assertFalse(Policy().accepts(A("q", "predicate", status="refused")))
        self.assertTrue(Policy().accepts(A("q", "predicate", False)))
        self.assertFalse(Policy(.9).accepts(A("q", "predicate", True)))
        self.assertTrue(Policy(.9).accepts(A("q", "predicate", True, probability=.9)))
        self.assertFalse(Policy(.9).accepts(A("q", "predicate", False, probability=.1)))
        self.assertFalse(Policy(.8).accepts(A("q", "choice", "a")))
        a = A("q", "choice", "a", probabilities=(("a", .8), ("b", .2)))
        self.assertTrue(Policy(.8, .5).accepts(a))
        self.assertFalse(Policy(.8, .7).accepts(a))


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_address_questions_are_independent_and_match_by_name(self):
        backend = NS(probabilistic=True, evaluate=AsyncMock())
        classifier = AddressClassifier(backend, policy=Policy(.9))
        for mention, reply, expected in (
            (A("mentions_self", "predicate", True, probability=.99), A("expects_reply", "predicate", False, probability=.1), False),
            (A("mentions_self", "predicate", False, probability=.01), A("expects_reply", "predicate", True, probability=.95), True),
            (A("mentions_self", "predicate", status="refused"), A("expects_reply", "predicate", True, probability=.95), True),
            (A("mentions_self", "predicate", True, probability=1), A("expects_reply", "predicate", True, probability=.89), False),
            (A("mentions_self", "predicate", True, probability=1), A("expects_reply", "predicate", status="unavailable"), False),
        ):
            backend.evaluate.return_value = Result((reply, mention))
            self.assertEqual(await classifier.expects_reply(self.snapshot(), (), self.event()), expected)
        backend.evaluate.return_value = Result((A("expects_reply", "predicate", True, probability=1),))
        with self.assertRaises(ValueError):
            await classifier.expects_reply(self.snapshot(), (), self.event())

    def event(self, id="m1"):
        return NS(id=id, author_id="alice", author_name="Alice", reply_to=None, response_to=None,
            reply_author_name=None, reply_text=None, called_name=False, text="hello", author_is_bot=False)

    def snapshot(self):
        return NS(persona="agent", habitus="", mood=NS(to_dict=lambda: {}))

    async def test_domain_classifiers(self):
        backend = NS(probabilistic=True, max_choices=255, evaluate=AsyncMock())
        backend.evaluate.return_value = Result((A("mentions_self", "predicate", False, probability=.1), A("expects_reply", "predicate", True, probability=.95)))
        self.assertTrue(await AddressClassifier(backend, policy=Policy(.9)).expects_reply(self.snapshot(), (), self.event()))
        request = backend.evaluate.call_args.args[0]
        self.assertEqual([q.name for q in request.questions], ["mentions_self", "expects_reply"])
        backend.evaluate.return_value = Result((A("mentions_self", "predicate", True, probability=.99), A("expects_reply", "predicate", status="refused")))
        self.assertFalse(await AddressClassifier(backend).expects_reply(self.snapshot(), (), self.event()))
        backend.probabilistic=False
        with self.assertRaises(ValueError): AddressClassifier(backend, policy=Policy(.9))
        with self.assertRaises(ValueError): EngagementClassifier(backend, speech_policy=Policy(.9))
        backend.probabilistic=True
        classifier=EngagementClassifier(backend, reaction_policy=Policy(.9))
        backend.evaluate.return_value = Result((A("engagement", "choice", "c1", probabilities=(("c0", .02),("c1", .98))),))
        value=await classifier.classify(self.snapshot(), (self.event(),), ("happy",))
        self.assertEqual(value, {"action":"react", "target":"m1", "face":"happy"})
        backend.evaluate.return_value = Result((A("engagement", "choice", "c1", probabilities=(("c0", .4),("c1", .6))),))
        self.assertEqual((await classifier.classify(self.snapshot(), (self.event(),), ("happy",)))["action"],"none")
        self.assertEqual((await classifier.classify(self.snapshot(), (), (), allow_react=False))["action"],"none")
        backend.evaluate.return_value = Result((A("engagement", "choice", "c1"),))
        self.assertEqual((await EngagementClassifier(backend).classify(self.snapshot(), (self.event(),), (), allow_react=False,allow_speak=True))["action"],"speak")
        backend.max_choices=1
        with self.assertRaises(ValueError): await classifier.classify(self.snapshot(), (self.event(),), ("happy",))

    async def test_decisions_wire(self):
        qs=(Q("p","predicate"),Q("c","choice","choice",("a","b")),Q("s","score","score",("low","high")),Q("r","refusal"))
        body={"model":"test","answers":[{"name":"c","type":"choice","choice":"a","confidence":.8,"probabilities":[{"value":"a","probability":.8},{"value":"b","probability":.2}]},
            {"name":"p","type":"predicate","probability":.98},
            {"name":"s","type":"score","score":.2,"confidence":.8,"probabilities":[{"label":"low","probability":.8},{"label":"high","probability":.2}]},
            {"name":"r","type":"refusal"}],"usage":{"input_tokens":10}}
        client=NS(post=AsyncMock(return_value=body))
        backend=OpenAIDecisionBackend(client=client)
        self.assertEqual((await backend.evaluate(R("synthetic",qs))).input_tokens,10)
        self.assertEqual(client.post.call_args.kwargs["body"]["questions"][2]["levels"],[{"label":"low"},{"label":"high"}])
        with self.assertRaises(ValueError): await backend.evaluate(R("",(Q("c","","choice",tuple(map(str,range(256)))),)))
        body["answers"][0]["type"]="unknown"
        with self.assertRaises(ValueError): await backend.evaluate(R("",qs))
        for error in [APITimeoutError(request=httpx.Request("POST","https://example.invalid")),
                APIConnectionError(request=httpx.Request("POST","https://example.invalid"))]:
            client.post.side_effect=error
            with self.assertRaises(TransientExternalError): await backend.evaluate(R("",qs))
        client.post.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await backend.evaluate(R("",qs))
        from anima.core.model_contracts import ModelBackendError
        for code,reason in [(401,"authentication"),(429,"rate_limit"),(500,"provider_failure")]:
            client.post.side_effect=APIStatusError("sensitive",response=httpx.Response(code,request=httpx.Request("POST","https://example.invalid")),body=None)
            with self.assertRaises(ModelBackendError) as caught: await backend.evaluate(R("",qs))
            self.assertEqual(caught.exception.reason,reason)
            self.assertNotIn("sensitive",str(caught.exception))

    async def test_real_sdk_transport_parsing(self):
        from openai import AsyncOpenAI
        def serve(request):
            self.assertEqual(request.url.path,"/v1/decisions")
            return httpx.Response(200,json={"model":"gpt-6-luna","answers":[
                {"type":"predicate","name":"p","probability":.98}],"usage":{"input_tokens":12}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            async with AsyncOpenAI(api_key="synthetic",http_client=http,max_retries=0) as client:
                result=await OpenAIDecisionBackend(client=client).evaluate(R("synthetic",(Q("p","is addressed?"),)))
        self.assertTrue(result.answers[0].value)
        self.assertEqual(result.input_tokens,12)

    async def test_responses_compatibility(self):
        response=NS(output_text='{"p":true,"c":"a","s":1}',output=(),usage=None)
        client=NS(responses=NS(create=AsyncMock(return_value=response)))
        backend=OpenAIResponseDecisionBackend(client=client,model="test")
        request=R("",(Q("p",""),Q("c","","choice",("a","b")),Q("s","","score",("low","high"))))
        result=await backend.evaluate(request)
        self.assertIsNone(result.answers[0].probability)
        response.output=(NS(content=(NS(type="refusal"),)),)
        self.assertEqual((await backend.evaluate(request)).answers[0].status,"refused")
        response.output=();response.output_text='{}'
        with self.assertRaises(ValueError): await backend.evaluate(request)

    async def test_session_isolation(self):
        async def request(*args): return NS(output=(),output_text="done")
        backend=OpenAIConversationBackend()
        spec=OpenAIConversationRequest([],[],request)
        first,second=backend.open(spec),backend.open(spec)
        self.assertIsNot(first.transcript,second.transcript)
        self.assertEqual((await first.think(1,False,())).result.output_text,"done")

    async def test_neutral_conversation_and_tool_loop(self):
        from anima.core.agentic_loop import AgenticLoop, ToolObservation
        from anima.capabilities.contracts import CapabilityConfigurationError
        spec=ConversationRequest("rules",({"role":"user","content":"hello"},))
        with self.assertRaises(ValueError): OpenAIConversationBackend().open(spec)
        client=NS(responses=NS(create=AsyncMock(return_value=NS(output=(),output_text="hello",usage=None))))
        backend=OpenAIConversationBackend(client=client,model="test")
        self.assertIsInstance((await backend.open(spec).think(1,False,())).result,ConversationResult)
        schema={"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False}
        spec=ConversationRequest("rules",(),({"type":"function","name":"echo","parameters":schema},),schema)
        call=NS(type="function_call",call_id="call1",name="echo",arguments='{"text":"x"}')
        client.responses.create.side_effect=[NS(output=(call,),output_text=""),NS(output=(),output_text='{"text":"done"}',usage=NS(input_tokens=4,output_tokens=2))]
        executor=NS(execute=AsyncMock(side_effect=lambda c:ToolObservation(c,'{"ok":true}')))
        result=await AgenticLoop(max_tool_rounds=1).run(backend=backend.open(spec),executor=executor)
        self.assertEqual(result.result.value,{"text":"done"})
        self.assertEqual(result.tool_call_count,1)
        self.assertNotIn("tools",client.responses.create.call_args.kwargs)
        client.responses.create.side_effect=None
        client.responses.create.return_value=NS(output=(),output_text='{"text":1}',usage=None)
        with self.assertRaises(ValueError): await backend.open(spec).think(1,False,())
        client.responses.create.return_value=NS(output=(NS(content=(NS(type="refusal"),)),),output_text="",usage=None)
        self.assertTrue((await backend.open(spec).think(1,False,())).result.refused)
        with self.assertRaises(CapabilityConfigurationError): backend.open(ConversationRequest("",(),output_schema={"type":"invalid"}))

    async def test_settings_and_independent_factory(self):
        with TemporaryDirectory() as tmp:
            env={"DISCORD_BOT_TOKEN":"test","OPENAI_API_KEY":"test","ANIMA_DECISION_BACKEND":"decisions"}
            settings=Settings.load(cwd=Path(tmp),environ=env)
            self.assertEqual(settings.decision_model,"gpt-6-luna")
            address, engagement=decision_classifiers(settings,NS())
            self.assertIs(address.backend,engagement.backend)
            external=NS(probabilistic=True,max_choices=100)
            a,_=decision_classifiers(settings,NS(),decision_factory=lambda *_:external)
            self.assertIs(a.backend,external)
            self.assertEqual(a.policy.threshold,.9)
            external.probabilistic=False
            with self.assertRaises(ValueError): decision_classifiers(settings,NS(),decision_factory=lambda *_:external)
            for name,value in [("ANIMA_DECISION_BACKEND","bad"),("ANIMA_DECISION_ADDRESS_THRESHOLD","nan"),
                    ("ANIMA_DECISION_SPEECH_THRESHOLD","2"),("ANIMA_DECISION_REACTION_THRESHOLD","bad")]:
                with self.assertRaises(ConfigurationError): Settings.load(cwd=Path(tmp),environ={**env,name:value})
            settings=Settings.load(cwd=Path(tmp),environ={"DISCORD_BOT_TOKEN":"test","OPENAI_API_KEY":"test"})
            a,_=decision_classifiers(settings,NS())
            self.assertIsInstance(a.backend,OpenAIResponseDecisionBackend)

    async def test_shadow_bounded_and_nonblocking(self):
        from anima.core.decision_shadow import ShadowDecisionBackend
        request=R("",(Q("p",""),))
        result=Result((A("p","predicate",True),))
        primary=NS(probabilistic=False,max_choices=255,evaluate=AsyncMock(return_value=result))
        gate=asyncio.Event()
        async def delayed(_):
            await gate.wait()
            return Result((A("p","predicate",False),))
        secondary=NS(evaluate=AsyncMock(side_effect=delayed))
        reserve=NS(calls=0)
        def budget(): reserve.calls+=1;return reserve.calls<=2
        backend=ShadowDecisionBackend(primary,secondary,reserve=budget,semaphore=asyncio.Semaphore(1))
        await backend.start()
        self.assertIs(await backend.evaluate(request),result)
        self.assertIs(await backend.evaluate(request),result)
        self.assertEqual(reserve.calls,1)
        gate.set()
        await asyncio.gather(*backend.tasks)
        secondary.evaluate.side_effect=RuntimeError("failed")
        await backend.evaluate(request)
        await asyncio.gather(*backend.tasks)
        await backend.evaluate(request)
        self.assertFalse(backend.tasks)
        await backend.stop()
        await backend.evaluate(request)
        self.assertFalse(backend.tasks)
        await backend.start()
        backend.reserve=lambda:True
        gate.clear();secondary.evaluate.side_effect=delayed
        await backend.evaluate(request)
        await backend.stop()
        await backend.start()
        def broken_budget(): raise OSError("storage unavailable")
        backend.reserve=broken_budget
        self.assertIs(await backend.evaluate(request),result)
        backend.reserve=lambda:True
        backend.timeout=.001
        await backend.evaluate(request)
        await asyncio.gather(*backend.tasks)
        await backend.stop()

    async def test_shadow_scoped_budget(self):
        from dataclasses import replace
        from anima.core.state import FileStateStore
        with TemporaryDirectory() as tmp:
            settings=Settings.load(cwd=Path(tmp),environ={"DISCORD_BOT_TOKEN":"test","OPENAI_API_KEY":"test"})
            settings=replace(settings,decision_shadow_backend="decisions",decision_shadow_daily_limit=1)
            with self.assertRaises(ValueError): decision_classifiers(settings,NS())
            store=FileStateStore(Path(tmp)/"state")
            a,_=decision_classifiers(settings,NS(),store=store,semaphore=asyncio.Semaphore(1))
            self.assertTrue(a.backend.reserve())
            self.assertFalse(a.backend.reserve())
            store._atomic_write_json(store.root/"runtime/decision-shadow.json",{"day":"2000-01-01","attempts":20})
            self.assertTrue(a.backend.reserve())
            a,_=decision_classifiers(replace(settings,decision_shadow_backend="responses"),NS(),store=store,semaphore=asyncio.Semaphore(1))
            self.assertIsInstance(a.backend.secondary,OpenAIResponseDecisionBackend)

    async def test_mock_discord_with_independent_backends(self):
        from test_app import settings_at
        from anima.bootstrap.app import build_sandbox
        from anima.core.sandbox import SandboxKey
        from anima.core.access import ActivityModeStore
        from anima.capabilities.plugin_loader import PluginLoader
        from anima.adapters.stub.discord import StubDiscordSender
        from anima.core.models import Event, ResponseDraft, OutcomeKind
        from datetime import datetime, timezone
        with TemporaryDirectory() as tmp:
            settings=settings_at(Path(tmp))
            responder=NS(respond=AsyncMock(return_value=ResponseDraft("other provider", "calm", "test", "ふつう", "")),reload_configuration=lambda **_:None)
            factory=NS(responder=lambda *_args,**_kw:responder,maintainer=lambda *_:NS(),self_time=lambda *_args,**_kw:NS())
            external=NS(probabilistic=False,max_choices=255,evaluate=AsyncMock(return_value=Result((A("mentions_self","predicate",False),A("expects_reply","predicate",True)))))
            key=SandboxKey("guild","1");sender=StubDiscordSender(clock=lambda:datetime.now(timezone.utc))
            with patch("anima.bootstrap.app.AsyncOpenAI"):
                runtime=build_sandbox(settings,key,key.path(settings.state_root),sender,ActivityModeStore(settings.state_root),PluginLoader().load(),asyncio.Semaphore(4),lambda:None,
                    conversation_factory=factory,decision_factory=lambda *_:external)
                await runtime.plugins.start();await runtime.actor.start()
                try:
                    event=Event("one",datetime.now(timezone.utc),"channel","10","general","20","Tester","hello",mention=True,guild_id="1",sandbox_key="guild:1")
                    outcome=await runtime.actor.submit(event)
                    self.assertEqual(outcome.kind,OutcomeKind.SPOKE)
                    self.assertEqual(sender.deliveries[-1].text,"other provider")
                    self.assertTrue(await runtime.actor.address_classifier.expects_reply(runtime.actor.store.load_snapshot(event),(),event))
                    self.assertIs(runtime.actor.address_classifier.target.backend,external)
                    runtime.reload_configuration()
                finally:
                    await runtime.actor.stop();await runtime.plugins.stop()


if __name__ == "__main__": unittest.main()
