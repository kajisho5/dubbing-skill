"""SKILL.md (roadmap step 9, ADR-012) stays true to the code: valid front matter, every command it names exists and every
command exists in it, every flag it names is a real option, every error code it names is in the error table, and the
consent rule it states (no bypass) is the code's."""
from __future__ import annotations

import argparse
import re
import unittest

from helpers import ROOT

from dubbing_skill import cli
from dubbing_skill.contract import contract
from dubbing_skill.engines.base import STATUSES
from dubbing_skill.errors import ERROR_TABLE

SKILL_MD = ROOT / "SKILL.md"


def subparsers() -> dict:
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subcommands")


class SkillMdTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL_MD.read_text(encoding="utf-8")

    def test_front_matter(self):
        m = re.match(r"^---\n(.*?)\n---\n", self.text, re.S)
        self.assertIsNotNone(m, "SKILL.md starts with YAML front matter")
        fields = dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)
        self.assertEqual(fields.get("name"), "dubbing")
        self.assertEqual(fields["name"], contract()["skill_id"])
        self.assertLessEqual(len(fields["description"]), 1024)
        for word in ("NARRATE", "DUB", "AUDIO_DESCRIPTION", "consent", "does not translate"):
            self.assertIn(word, fields["description"])

    def test_every_command_is_documented_and_exists(self):
        commands = set(subparsers())
        named = set(re.findall(r"`dubbing ([a-z]+)", self.text)) | set(re.findall(r"`([a-z]+) --json`", self.text))
        self.assertTrue(named, "SKILL.md names commands")
        self.assertEqual(sorted(named - commands), [], "SKILL.md names a command the CLI does not have")
        self.assertEqual(sorted(commands - named), [], "a CLI command is missing from SKILL.md")

    def test_every_flag_it_names_is_real(self):
        options = {"--json"}
        for sub in subparsers().values():
            for a in sub._actions:
                options.update(a.option_strings)
        named = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", self.text))
        self.assertEqual(sorted(named - options), [], "SKILL.md names a flag no command takes")

    def test_every_error_code_it_names_is_real(self):
        named = set(re.findall(r"`([A-Z][A-Z_]{3,})`", self.text))
        others = {"NARRATE", "DUB", "AUDIO_DESCRIPTION"} | set(STATUSES)          # op names and doctor statuses
        self.assertEqual(sorted(named - set(ERROR_TABLE) - others), [])
        for code in ("CONSENT_REQUIRED", "TIMING_OVERFLOW", "FFMPEG_SKILL_UNAVAILABLE", "TERMS_UNVERIFIED"):
            self.assertIn(code, named)

    def test_the_consent_rule_matches_the_code(self):
        self.assertIn("no flag that skips it", self.text)
        self.assertIsNone(contract()["policy"]["consent_record"]["bypass"])
        self.assertIn("dubbing/consent@1", self.text)


if __name__ == "__main__":
    unittest.main()
