import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from anima.core.attention import AttentionPolicy
from anima.core.context import ContextBuilder
from anima.core.models import Event, ResponseDraft, SentMessage, MentionedPerson
from anima.core.state import FileStateStore
from anima.adapters.openai.client import _compact_tool_instructions
from test_sandbox import event


class ConversationAddressingTests(unittest.TestCase):
    def test_event_compatibility_validation(self):
        source = event()
        self.assertIsNone(Event.from_log_dict(source.to_log_dict()).response_to)
        linked = replace(source, response_to='parent')
        self.assertEqual(Event.from_log_dict(linked.to_log_dict()), linked)
        for invalid in ('', '../bad', 42):
            with self.assertRaises(ValueError):
                replace(source, response_to=invalid)

    def test_actual_thanks_and_two_same_named_speakers_are_not_merged(self):
        a = replace(event(), id='a', author_id='a', author_name='same', text='いつも素敵なアイコンありがとう💕', mention=False)
        b = replace(a, id='b', author_id='b', text='私が描いたよ')
        rows = ContextBuilder()._input((a, b), a.ts)
        self.assertIn('"author_id": "a"', rows[0]['content'])
        self.assertIn('"author_id": "b"', rows[1]['content'])
        self.assertIn('他人の制作・保存を自分の行為として語らない', ContextBuilder._response_section(a, False))
        # Reply to a person with an explicit Bot mention: requester remains A, not B.
        request = replace(a, mention=True, reply_to='b', reply_author_name='same', reply_text=b.text)
        self.assertIn('"person_id": "a"', ContextBuilder._response_section(request, False))
        self.assertIn('same: 私が描いたよ', ContextBuilder()._input((b, request), a.ts)[1]['content'])

    def test_selection_pins_source_and_bounded_ancestors(self):
        policy = AttentionPolicy(5, 2, 3)
        rows = tuple(replace(event(), id=str(i), reply_to=str(i-1) if i else None) for i in range(12))
        self.assertEqual([e.id for e in policy.select(rows, rows[4], occupied=False)], ['1','2','3','4','11'])
        self.assertEqual([e.id for e in policy.select(rows, rows[4], occupied=True)], ['2','3','4'])
        self.assertEqual(policy.select(rows, None, occupied=False), rows[-5:])
        ambient = replace(rows[4], mention=False)
        self.assertEqual(len(policy.select(rows, ambient, occupied=True)), 2)
        cycle = (replace(rows[0], reply_to='1'), replace(rows[1], reply_to='0'))
        self.assertEqual(len(policy.select(cycle, cycle[0], occupied=False)), 2)
        source = replace(rows[0], id='missing', reply_to='unknown')
        foreign = (replace(rows[0], channel_id='other'), replace(rows[0], guild_id='2'),
                   replace(rows[0], sandbox_key='guild:2'))
        self.assertEqual(policy.select(foreign, source, occupied=False), (source,))

    def test_store_context_and_proactive_relationships(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(Path(directory), recent_limit=2)
            source = replace(event(), id='a', author_name='same', text='どう？')
            store.ensure_layout(now=source.ts)
            store.append_received(source)
            store.commit_response(source=source, draft=ResponseDraft('返事', '普通', '', '弱い', ''),
                                  sent=SentMessage('bot', source.ts), expected_version=0)
            own = store.read_channel_events('100')[-1]
            self.assertEqual(own.response_to, source.id)
            self.assertIsNone(own.reply_to)
            b = replace(source, id='b', author_id='other', text='別の話')
            store.append_received(b)
            target = replace(source, id='target', reply_to='bot')
            store.append_received(target)
            snapshot = store.load_snapshot(target)
            self.assertEqual([e.id for e in snapshot.recent_events], ['a','bot','b','target'])
            context = ContextBuilder().build(snapshot, now=source.ts, source_event=target)
            self.assertIn('"event_id": "target"', context.instructions)
            self.assertIn('"response_to": "a"', str(context.input))
            store.append_received(replace(b, id='c'))
            snapshot = store.load_snapshot(target)
            with patch('anima.core.context.emit') as emit:
                context = ContextBuilder().build(snapshot, now=source.ts, source_event=target)
            self.assertEqual(emit.call_args.kwargs['background_count'], 1)
            self.assertIn('"background": true', context.input[-1]['content'])
            compressed = _compact_tool_instructions('X'*4000+'\n\n'+context.instructions)
            self.assertIn('"event_id": "target"', compressed)
            self.assertIn('# いま話している相手', compressed)
            proactive = ContextBuilder().build(snapshot, now=source.ts, source_event=target, proactive=True)
            self.assertIn('"kind": "proactive"', proactive.instructions)
            self.assertIn('"event_id": null', proactive.instructions)
            store.commit_response(source=target, draft=ResponseDraft('独り言', '普通', '', '弱い', ''),
                                  sent=SentMessage('proactive', source.ts), expected_version=1, proactive=True)
            self.assertIsNone(store.read_channel_events('100')[-1].response_to)

    def test_missing_source_deleted_quote_and_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(Path(directory), recent_limit=1)
            source = event()
            store.ensure_layout(now=source.ts)
            store.append_received(source)
            target = replace(source, id='target', reply_to=source.id, reply_text='deleted secret')
            store.append_received(target)
            store.mark_deleted(source.channel_id, source.id)
            snapshot = store.load_snapshot(target)
            context = ContextBuilder().build(snapshot, now=source.ts, source_event=target)
            self.assertNotIn('deleted secret', str(context.input))
            unknown = replace(source, id='not-logged', reply_to='unknown')
            snapshot = store.load_snapshot(unknown)
            self.assertEqual(snapshot.recent_events[0].id, unknown.id)
            context = ContextBuilder().build(snapshot, now=source.ts, source_event=unknown)
            self.assertIn('"background": true', str(context.input))
            note = replace(unknown, response_target=MentionedPerson('person', 'User'))
            section = ContextBuilder._response_section(note, False)
            self.assertIn('"kind": "notification"', section)
            self.assertIn('"person_id": "person"', section)
            self.assertIn('"event_id": null', ContextBuilder._response_section(None, False))
