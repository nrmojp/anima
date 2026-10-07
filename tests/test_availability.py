import unittest

from anima.capabilities.availability import Availability, AvailabilityStatus


class AvailabilityTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(AvailabilityStatus(Availability.AVAILABLE).state.value, "available")
        self.assertEqual(
            AvailabilityStatus(Availability.UNAVAILABLE, "Try again later.").reason,
            "Try again later.",
        )
        with self.assertRaises(TypeError):
            AvailabilityStatus("available")
        for reason in ("", "x" * 201):
            with self.subTest(reason=reason[:10]), self.assertRaises(ValueError):
                AvailabilityStatus(Availability.DISABLED, reason)


if __name__ == "__main__":
    unittest.main()
