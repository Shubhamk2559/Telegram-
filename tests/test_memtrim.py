import unittest
from src import memtrim


class Memtrim(unittest.TestCase):
    def test_trim_never_raises(self):
        memtrim.trim()
        memtrim.trim_and_log("test")

    def test_rss_is_a_number_on_linux(self):
        v = memtrim.rss_mb()
        self.assertTrue(v is None or v > 0)


if __name__ == "__main__":
    unittest.main()
