"""Conservative static patch-data exemption; executable syntax stays UNKNOWN."""
import json
from unittest import mock

import herdr_codex_children as cc
from test_codex_child_guard import Base, P, rec, write_lines


def wrapper(value, sink="apply_patch"):
    return "text(await tools." + sink + "(" + json.dumps(value) + "));"


def custom(text, **extra):
    return {"type": "custom_tool_call", "name": "exec", "input": text, **extra}


class LiteralPatchRecognition(Base):
    def test_observed_patch_then_command_shape_is_data(self):
        # JSON-encoded counterpart of the retained call; private patch removed.
        # Actual unquoted adjacent command keys have their own positive case.
        text = wrapper('*** Begin Patch\n+Use ONE collaboration.spawn_agent call\n*** End Patch')
        text += "\n" + wrapper({"cmd": "python3 inspect_gauge.py", "yield_time_ms": 1000}, "exec_command")
        cc._custom_call(custom(text))
        spans = cc._literal_patch_spans(text)
        self.assertEqual(len(spans), 2)
        self.assertEqual(json.loads(text[slice(*spans[0])]).splitlines()[1], "+Use ONE collaboration.spawn_agent call")

    def test_json_escaped_content_and_multiple_patch_strings(self):
        for content in ('tools.spawn_agent("quote")', 'line\\n tools.spawn_agent\n', '雪 collaboration.spawn_agent',
                        '"; tools.spawn_agent({}); //', '/* tools.spawn_agent */', 'spawn_agent('):
            with self.subTest(content=content):
                cc._custom_call(custom(wrapper(content) + "\n\t" + wrapper(content)))

    def test_observed_unquoted_adjacent_command_key_is_static(self):
        text = wrapper("collaboration.spawn_agent") + '\ntext(await tools.exec_command({cmd:"inspect_gauge.py"}));'
        cc._custom_call(custom(text))

    def test_static_object_keys_values_and_boundaries(self):
        good = wrapper("collaboration.spawn_agent")
        for value in ('{cmd:"inspect", "n":4,flag:true,items:[1,"two"],nested:{"key":null}}', '{}'):
            cc._custom_call(custom(good + 'text(await tools.inspect(' + value + '));'))
        bad = ('{cmd:run()}', '{cmd:`interpolate${run()}`}', '{[key]:"inspect"}',
               '{...spread}', '{get cmd(){return "x"}}', '{cmd}', '{cmd:"x",}',
               '{cmd:"x",cmd:"y"}', '{"cmd":"x",cmd:"y"}', '{__proto__:{}}',
               '{"constructor":{}}', '{nested:{"prototype":{}}}', "{'cmd':\"x\"}",
               '{cmd:"x"/*comment*/}', '{cmd:undefined}', '{cmd:NaN}', '{cmd:Infinity}',
               '{cmd:"x"+"y"}', '{nested:{key:"x"}}', '{cmd:0x20}')
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(cc._Unknown):
                    cc._custom_call(custom(good + 'text(await tools.exec_command(' + value + '));'))
        members = ','.join('k'+str(i)+':0' for i in range(4096))
        cc._custom_call(custom(good + 'text(await tools.inspect({' + members + '}));'))
        with self.assertRaises(cc._Unknown):
            cc._custom_call(custom(good + 'text(await tools.exec_command({' + members + ',extra:0}));'))

    def test_static_cmd_is_literal_data_not_a_native_agent_expression(self):
        text = wrapper("collaboration.spawn_agent") + '\ntext(await tools.exec_command({cmd:"tools.spawn_agent({})"}));'
        cc._custom_call(custom(text))
        for arg in ('"tools.spawn_agent({})"', '{cmd:"tools.spawn_agent({})",agent_type:"other"}',
                    '{cmd:tools.spawn_agent({})}', '{cmd:"x"+tools.spawn_agent({})}',
                    '{cmd:()=>tools.spawn_agent({})}'):
            with self.subTest(arg=arg), self.assertRaises(cc._Unknown):
                cc._custom_call(custom('text(await tools.exec_command(' + arg + '));'))

    def test_actual_expressions_and_other_string_sinks_are_not_exempt(self):
        cases = [
            'text(await tools.spawn_agent({}));',
            'text(await tools.apply_patch(tools.spawn_agent({})));',
            wrapper("tools.spawn_agent({})", "eval"),
            'const x="tools.spawn_agent({})";',
            'eval(' + json.dumps(wrapper("tools.spawn_agent({})")) + ');',
            'Function(' + json.dumps(wrapper("tools.spawn_agent({})")) + ')();',
            'text(await tools["apply_patch"]("tools.spawn_agent({})"));',
            'text(await tools.apply_patch.call(null,"tools.spawn_agent({})"));',
            'text(await tools.apply_patch(`tools.spawn_agent({})`));',
            'text(await tools.apply_patch(`a${tools.spawn_agent({})}`));',
            'text(await tools.apply_patch("tools.spawn_agent({})"+more));',
            'text(await tools.apply_patch("tools.spawn_agent({})",extra));',
            '/*' + wrapper("tools.spawn_agent({})") + '*/ eval(payload);',
            '// ' + wrapper("tools.spawn_agent({})"),
            'other' + wrapper("tools.spawn_agent({})"),
            wrapper("tools.spawn_agent({})", "apply_patch_evil"),
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(cc._Unknown):
                    cc._custom_call(custom(text))

    def test_entire_wrapper_must_validate_before_any_exemption(self):
        good = wrapper("tools.spawn_agent({})")
        for bad in (good + "eval(payload);", good + "// suffix", good[:-1], good[:-3], good + "text(await tools.other(NaN));",
                    good + "text(await tools.other({broken}));", good + "text(await tools.apply_patch(3));",
                    'text(await tools.apply_patch("unterminated tools.spawn_agent({})));'):
            with self.subTest(text=bad):
                with self.assertRaises(cc._Unknown):
                    cc._custom_call(custom(bad))

    def test_non_patch_match_before_or_after_allowed_span_still_stops(self):
        good = wrapper("tools.spawn_agent({})")
        bad = wrapper("tools.spawn_agent({})", "exec_command")
        for text in (good + bad, bad + good, good + bad + good):
            with self.assertRaises(cc._Unknown):
                cc._custom_call(custom(text))

    def test_exact_exec_envelope_only_and_no_dynamic_sink(self):
        text = wrapper("collaboration.spawn_agent")
        for extra in ({"name": "apply_patch"}, {"namespace": "other"}, {"namespace": "collaboration"}, {"name": "spawn_agent"}):
            with self.assertRaises(cc._Unknown):
                cc._custom_call(custom(text, **extra))
        cc._custom_call(custom(text, namespace="functions"))

    def test_wrapper_statement_budget_and_recursion_failure_are_conservative(self):
        one = wrapper("tools.spawn_agent({})")
        cc._custom_call(custom(one * 4096))
        with self.assertRaises(cc._Unknown):
            cc._custom_call(custom(one * 4097))
        deep = one + 'text(await tools.other(' + '[' * 10000 + '1' + ']' * 10000 + '));'
        with self.assertRaises(cc._Unknown):
            cc._custom_call(custom(deep))

    def inject(self, **options):
        made = self.world.build(**options)
        rows = [json.loads(line) for line in made["parent"].read_text().splitlines()]
        rows.insert(1, rec("response_item", custom(wrapper("collaboration.spawn_agent")), 1))
        write_lines(made["parent"], rows)
        return made

    def test_literal_patch_does_not_prove_zero_spawns(self):
        self.inject()
        self.assertEqual(cc.verify(P, self.root).kind, cc.COMPLETE)
        self.inject(second_spawn=True)
        self.assertEqual(cc.verify(P, self.root).kind, cc.VIOLATION)
        self.inject(spawn_output="refused", child=False, mailbox="none")
        result = cc.verify(P, self.root)
        self.assertFalse(result.clear)
        self.assertEqual(result.evidence["spawn_calls"], 1)

    def test_transparency_requires_rest_of_history_and_discovery(self):
        made = self.inject(spawn="none", child=False, mailbox="none")
        self.assertEqual(cc.verify(P, self.root).kind, cc.TRANSPARENT)
        with made["parent"].open("ab") as stream:
            stream.write(b'{"type":"unsupported","payload":{}}\n')
        self.assertEqual(cc.verify(P, self.root).kind, cc.UNKNOWN)
        self.inject(spawn="none", child=False, mailbox="none")
        with mock.patch.object(cc, "_discover_descendants", side_effect=cc._Unknown("unreadable namespace")):
            result = cc.verify(P, self.root)
        self.assertEqual(result.kind, cc.UNKNOWN)
        self.assertIn("namespace", result.detail)
