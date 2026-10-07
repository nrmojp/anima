import unittest
from dataclasses import replace

from anima.core.attention import AttentionPolicy
from test_sandbox import event


class AttentionPolicyTests(unittest.TestCase):
    def test_windows_follow_occupation_and_direct_call(self):
        policy = AttentionPolicy(5, 2, 3, 0.5)
        events = tuple(replace(event(), id=str(index)) for index in range(6))
        direct = replace(events[-1], mention=True)
        ambient = replace(events[-1], mention=False)
        self.assertEqual(len(policy.select(events, direct, occupied=False)), 5)
        self.assertEqual(len(policy.select(events, direct, occupied=True)), 3)
        self.assertEqual(len(policy.select(events, ambient, occupied=True)), 2)
        self.assertEqual(policy.delay(direct, occupied=True), 0.5)
        self.assertEqual(policy.delay(direct, occupied=False), 0.0)
        self.assertEqual(policy.delay(None, occupied=True), 0.0)

    def test_configuration_is_validated(self):
        for arguments in ((0, 1, 1, 0), (3, 2, 4, 0), (3, 2, 2, -1)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                AttentionPolicy(*arguments)


if __name__ == "__main__":
    unittest.main()
