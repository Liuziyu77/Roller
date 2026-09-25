from copy import deepcopy
import unittest

from roller.actf import openclaw_events_to_actf
from roller.claude_code_actf import claude_code_events_to_actf
from roller.codex_actf import codex_events_to_actf, _arguments
from roller.pi_actf import pi_events_to_actf
from roller.sft import SFTConversionError, _normalize_tools, _normalize_tool_call, actf_to_sft


class MultimodalIntegrityTests(unittest.TestCase):
    def events(self, content):
        return [
            {"type": "message", "message": {"role": "user", "content": content}},
            {"type": "message", "message": {"role": "assistant", "content": "done"}},
        ]

    def test_preserves_native_text_image_text_order_and_roles(self):
        content = [{"type": "text", "text": "before"},
                   {"type": "image", "data": "AA==", "mimeType": "image/png"},
                   {"type": "text", "text": "after"}]
        original = deepcopy(content)
        actf = openclaw_events_to_actf(self.events(content))
        self.assertEqual(actf["steps"][0]["user_content"], original)
        sft = actf_to_sft(actf)
        self.assertEqual([m["role"] for m in sft["messages"]], ["system", "user", "assistant"])
        self.assertEqual(sft["messages"][1]["content"], "before\n<image>\nafter")
        self.assertEqual(sft["images"], ["data:image/png;base64,AA=="])
        self.assertEqual(content, original)

    def test_multiple_image_encodings_preserve_occurrence_order(self):
        content = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,AQ=="}}]
        sft = actf_to_sft(openclaw_events_to_actf(self.events(content)))
        self.assertEqual(sft["images"], ["data:image/png;base64,AA==", "data:image/png;base64,AQ=="])

    def test_orphan_result_is_not_silently_discarded(self):
        events = [{"type": "message", "message": {"role": "toolResult", "toolCallId": "lost", "content": "result"}}]
        with self.assertRaisesRegex(ValueError, "orphan"):
            openclaw_events_to_actf(events + self.events("hello"))

    def test_incomplete_projection_is_never_exported(self):
        trajectory = openclaw_events_to_actf(self.events("hello"))
        trajectory["projection"] = {"complete": False, "training_eligible": False}
        with self.assertRaisesRegex(SFTConversionError, "incomplete"):
            actf_to_sft(trajectory)

    def test_non_single_text_replacement_keeps_upstream_text_only_behavior(self):
        picture = {"type": "image", "data": "AA==", "mimeType": "image/png"}
        for content in ([picture], [{"type": "text", "text": "before"}, picture,
                                    {"type": "text", "text": "after"}]):
            with self.subTest(content=content):
                original = deepcopy(content)
                actf = openclaw_events_to_actf(self.events(content), user_messages=["different"])
                self.assertEqual(actf["steps"][0]["user_content"], "different")
                sft = actf_to_sft(actf)
                self.assertEqual(sft["messages"][1]["content"], "different")
                self.assertFalse(sft.get("images"))
                self.assertEqual(content, original)

    def test_prompt_cleanup_replaces_one_text_block_without_moving_images(self):
        from roller.actf import _user_content
        for original_text in ("wrapper\nclean prompt", {"type": "text", "text": "wrapper\nclean prompt", "extra": "keep"}):
            for image_first in (False, True):
                with self.subTest(text=original_text, image_first=image_first):
                    picture = {"type": "image", "data": "AA==", "mimeType": "image/png"}
                    content = [picture, original_text] if image_first else [original_text, picture]
                    original = deepcopy(content)
                    updated = _user_content(content, "clean prompt")
                    self.assertEqual(updated[0 if image_first else 1], picture)
                    self.assertEqual(content, original)
                    exported = actf_to_sft(openclaw_events_to_actf(self.events(content), user_messages=["clean prompt"]))
                    self.assertEqual(exported["messages"][1]["content"],
                                     "<image>\nclean prompt" if image_first else "clean prompt\n<image>")

    def test_codex_and_claude_keep_prompt_cleanup_with_native_image(self):
        from test_codex_actf import _events as codex_events
        from test_claude_code_actf import _events as claude_events
        cases = [(codex_events(), codex_events_to_actf, "read input.txt"),
                 (claude_events(), claude_code_events_to_actf, "inspect input.txt")]
        for events, project, prompt in cases:
            with self.subTest(project=project.__name__):
                if project is codex_events_to_actf:
                    content = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                               {"type": "input_text", "text": "wrapper\n" + prompt}]
                    events[2]["payload"]["content"] = content
                else:
                    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
                               {"type": "text", "text": " \n" + prompt + "\n "}]
                    events[0]["message"]["content"] = content
                actf = project(events, user_messages=[prompt])
                self.assertEqual(actf["steps"][0]["user_content"][0], content[0])
                self.assertEqual(actf["steps"][0]["user_content"][1]["text"], prompt)

    def test_unknown_media_and_unpersisted_urls_are_not_silently_lost(self):
        for block in [{"type": "video", "url": "local.mp4"},
                      {"type": "input_image", "image_url": "file:///gone.png"}]:
            with self.subTest(block=block), self.assertRaises(SFTConversionError):
                actf_to_sft(openclaw_events_to_actf(self.events([block])))

    def test_namespace_and_custom_tool_mapping_is_explicit(self):
        native = [{"type": "namespace", "name": "functions", "tools": [
            {"type": "function", "name": "read", "parameters": {"type": "object"}},
            {"type": "custom", "name": "patch", "format": {"type": "text"}},
        ]}]
        original = deepcopy(native)
        tools = _normalize_tools(native)
        self.assertEqual([t["function"]["name"] for t in tools], ["functions.read", "functions.patch"])
        self.assertEqual(tools[1]["function"]["parameters"]["required"], ["input"])
        call = _normalize_tool_call({"id": "1", "name": "read", "namespace": "functions", "arguments": {}}, "call")
        self.assertEqual(call["function"]["name"], "functions.read")
        self.assertEqual(native, original)
        with self.assertRaises(SFTConversionError):
            _normalize_tools(native + [{"type": "function", "name": "functions.read"}])

    def test_codex_preserves_user_media_and_custom_string_even_if_json(self):
        from test_codex_actf import _events
        events = _events()
        content = [{"type": "input_text", "text": "before"},
                   {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                   {"type": "input_text", "text": "after"}]
        events[2]["payload"]["content"] = content
        actf = codex_events_to_actf(events)
        self.assertEqual(actf["steps"][0]["user_content"], content)
        self.assertEqual(actf_to_sft(actf)["images"], ["data:image/png;base64,AA=="])
        native = '{"path":"file"}'
        self.assertEqual(_arguments({"type": "custom_tool_call", "input": native}), ({"input": native}, native))

    def test_claude_preserves_user_blocks(self):
        from test_claude_code_actf import _events
        events = _events()
        content = [{"type": "text", "text": "before"},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
                   {"type": "text", "text": "after"}]
        events[0]["message"]["content"] = content
        actf = claude_code_events_to_actf(events)
        self.assertEqual(actf["steps"][0]["user_content"], content)
        self.assertEqual(actf_to_sft(actf)["images"], ["data:image/png;base64,AA=="])

    def test_pi_user_image_and_incomplete_diagnostic(self):
        from test_pi import sample_events
        events = sample_events()
        content = [{"type": "text", "text": "before"},
                   {"type": "image", "mimeType": "image/png", "data": "AA=="},
                   {"type": "text", "text": "after"}]
        events[1]["event"]["message"]["content"] = content
        self.assertEqual(pi_events_to_actf(events)["steps"][0]["user_content"], content)
        partial = events[:4] + [{"type": "runner_error"}]
        with self.assertRaises(ValueError):
            pi_events_to_actf(partial)
        diagnostic = pi_events_to_actf(partial, allow_incomplete=True)
        self.assertFalse(diagnostic["projection"]["training_eligible"])
        with self.assertRaises(SFTConversionError):
            actf_to_sft(diagnostic)
