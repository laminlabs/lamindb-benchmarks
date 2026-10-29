"""Offline checks for exact prompt preservation and safe reuse."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SPEC = importlib.util.spec_from_file_location(
    "discovery_create_prompt", Path(__file__).resolve().parents[1] / "create_prompt.py"
)
prompts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prompts)


class CreatePromptTests(unittest.TestCase):
    def sdk(self, existing=None):
        ln = MagicMock()
        parent, experiment, frame = (MagicMock(is_type=True) for _ in range(3))
        frame.schema = None
        ln.Record.filter.return_value.one_or_none.side_effect = [
            parent,
            experiment,
            frame,
            existing,
        ]
        feature = MagicMock(dtype_as_str="str")
        ln.Feature.filter.return_value.one_or_none.return_value = feature
        return ln, parent, experiment, frame, feature

    def test_exact_text_and_scoped_hierarchy(self):
        ln, parent, experiment, frame, feature = self.sdk()
        text = '  Find human datasets.\r\nKeep "quotes" and Unicode: β.\n'
        result = prompts.create_prompt(ln, "  Human datasets  ", text)
        self.assertEqual(result, ln.Record.return_value.save.return_value)
        self.assertEqual(
            [call.kwargs for call in ln.Record.filter.call_args_list],
            [
                {"name": "Load Benchmarks", "type": None},
                {"name": "agent_dataset_discovery", "type": parent},
                {"name": "prompts", "type": experiment},
                {"name": "Human datasets", "type": frame},
            ],
        )
        ln.Record.assert_called_once_with(
            name="Human datasets", type=frame, features={feature: text}
        )
        ln.Schema.assert_called_once_with([feature], minimal_set=True)

    def test_same_name_and_text_reuses_record(self):
        record = MagicMock(is_type=False)
        record.features.get_values.return_value = {"prompt": "Find datasets"}
        ln, *_ = self.sdk(record)
        self.assertIs(prompts.create_prompt(ln, "Find", "Find datasets"), record)
        ln.Record.assert_not_called()

    def test_name_collision_does_not_overwrite(self):
        record = MagicMock(is_type=False)
        record.features.get_values.return_value = {"prompt": "Original"}
        ln, *_ = self.sdk(record)
        with self.assertRaisesRegex(ValueError, "different content"):
            prompts.create_prompt(ln, "Find", "New text")
        record.save.assert_not_called()
        record.features.set_values.assert_not_called()
        ln.Record.assert_not_called()

    def test_incompatible_feature_is_rejected(self):
        ln, *_, feature = self.sdk()
        feature.dtype_as_str = "int"
        with self.assertRaisesRegex(ValueError, "dtype str"):
            prompts.create_prompt(ln, "Find", "Find datasets")
        ln.Record.assert_not_called()
        ln.Schema.assert_not_called()

    def test_empty_inputs_rejected_before_database_access(self):
        for name, text in [(" ", "Text"), ("Name", "\n"), ("x" * 151, "Text")]:
            with self.subTest(name=name, text=text):
                ln = MagicMock()
                with self.assertRaises(ValueError):
                    prompts.create_prompt(ln, name, text)
                self.assertEqual(ln.mock_calls, [])

    def test_missing_type_is_created_under_parent(self):
        ln, parent = MagicMock(), MagicMock()
        ln.Record.filter.return_value.one_or_none.return_value = None
        prompts.ensure_type(ln, "prompts", parent)
        ln.Record.assert_called_once_with(name="prompts", type=parent, is_type=True)


if __name__ == "__main__":
    unittest.main()
