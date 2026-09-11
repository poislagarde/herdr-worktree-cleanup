import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disposable


class PatternTests(unittest.TestCase):
    def decision(self, text, paths):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patterns"
            path.write_text(text)
            return disposable.matches(paths, disposable.policy(path)[1])

    def test_patterns_comments_root_and_nested_directory_rules(self):
        decisions = self.decision("# comment\n/node_modules/\n**/.cache/\n",
                                  ["node_modules/a", "nested/node_modules/a", ".cache/a", "nested/.cache/a"])
        self.assertEqual(decisions, {"node_modules/a": "allow", ".cache/a": "allow", "nested/.cache/a": "allow"})

    def test_exceptions_inside_approved_directories(self):
        self.assertEqual(self.decision("cache/\n!cache/important/**\n",
                                      ["cache/a", "cache/important/data", "cache/important/sub/data"]),
                         {"cache/a": "allow", "cache/important/data": "protect", "cache/important/sub/data": "protect"})

    def test_ordered_overrides_and_escaped_patterns(self):
        self.assertEqual(self.decision("*.log\n!important.log\nimportant.log\n\\#cache\n\\!cache\n",
                                      ["debug.log", "important.log", "#cache", "!cache"]),
                         {"debug.log": "allow", "important.log": "allow", "#cache": "allow", "!cache": "allow"})

    def test_negations_of_literal_comment_and_negation_characters(self):
        self.assertEqual(self.decision("*\n!#keep\n!!keep\n", ["#keep", "!keep", "keep"]),
                         {"#keep": "protect", "!keep": "protect", "keep": "allow"})

    def test_brackets_question_mark_double_star_and_escaped_spaces(self):
        self.assertEqual(self.decision("**/cache[0-9]/?.tmp\nspace\\ \n",
                                      ["cache1/a.tmp", "sub/cache3/b.tmp", "cacheA/a.tmp", "cache1/ab.tmp", "space "]),
                         {"cache1/a.tmp": "allow", "sub/cache3/b.tmp": "allow", "space ": "allow"})

    def test_policy_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patterns"
            path.write_bytes(b"x" * (disposable.MAX_POLICY_BYTES + 1))
            with self.assertRaises(disposable.DisposalError):
                disposable.policy(path)
            path.write_text("x\n" * (disposable.MAX_RULES + 1))
            with self.assertRaises(disposable.DisposalError):
                disposable.policy(path)

    def test_invalid_git_matcher_output_fails_closed(self):
        original = disposable._command
        def invalid(argv, cwd, **kwargs):
            if argv[1] == "init":
                return original(argv, cwd, **kwargs)
            return "malformed\0"
        with patch.object(disposable, "_command", side_effect=invalid):
            with self.assertRaises(disposable.DisposalError):
                disposable.matches(["cache/file"], [("cache/", False)])

    def test_inventory_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "a").write_text("a")
            with patch.object(disposable, "MAX_ENTRIES", 0):
                with self.assertRaisesRegex(disposable.DisposalError, "entry limit"):
                    disposable.inventory(temporary)


if __name__ == "__main__":
    unittest.main()
