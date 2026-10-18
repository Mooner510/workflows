import unittest

from greeting import greet


class GreetingTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("stacks"), "hello stacks")


if __name__ == "__main__":
    unittest.main()
