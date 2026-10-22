import unittest

from app import greet


class GreetTest(unittest.TestCase):
    def test_greets_a_name(self):
        self.assertEqual("Hello, Continuum!", greet("Continuum"))

    def test_rejects_an_empty_name(self):
        with self.assertRaises(ValueError):
            greet("")


if __name__ == "__main__":
    unittest.main()
