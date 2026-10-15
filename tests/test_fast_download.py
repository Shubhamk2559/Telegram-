import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from src import fast_download as fd

PART = 1024  # test me chhota part


class FakeClient:
    """Telethon iter_download jaisa: offset se, limit chunks, request_size bytes ke chunk."""
    def __init__(self, data, fail_at=None, short=False):
        self.data, self.fail_at, self.short = data, fail_at, short
        self.calls = []

    def iter_download(self, media, *, offset=0, limit=None, request_size=PART, file_size=None):
        self.calls.append((offset, limit))
        client = self

        class It:
            def __aiter__(self_):
                async def gen():
                    pos, n = offset, 0
                    while pos < len(client.data) and (limit is None or n < limit):
                        await asyncio.sleep(0)
                        if client.fail_at is not None and pos >= client.fail_at:
                            raise RuntimeError("boom")
                        chunk = client.data[pos:pos + request_size]
                        if client.short and n == 1:
                            return
                        yield chunk
                        pos += len(chunk); n += 1
                return gen()
        return It()


def msg(size):
    return SimpleNamespace(file=SimpleNamespace(size=size), media=object())


class Parallel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "f.part")

    def tearDown(self):
        self.tmp.cleanup()

    async def run_dl(self, size, conns, **kw):
        data = os.urandom(size)
        c = FakeClient(data, **kw)
        prog = []
        await fd.parallel_download(c, msg(size), self.path, conns, lambda d, t: prog.append((d, t)), part=PART)
        return data, c, prog

    async def test_identical_bytes_odd_size(self):
        data, c, prog = await self.run_dl(PART * 10 + 123, 4)
        self.assertEqual(open(self.path, "rb").read(), data)
        self.assertEqual(len(c.calls), 4)
        self.assertEqual(prog[-1], (len(data), len(data)))

    async def test_offsets_are_part_aligned(self):
        _, c, _ = await self.run_dl(PART * 9, 3)
        self.assertTrue(all(off % PART == 0 for off, _ in c.calls))

    async def test_fewer_parts_than_connections(self):
        data, c, _ = await self.run_dl(PART * 2, 8)
        self.assertEqual(open(self.path, "rb").read(), data)
        self.assertEqual(len(c.calls), 2)

    async def test_worker_error_propagates(self):
        with self.assertRaises(RuntimeError):
            await self.run_dl(PART * 12, 4, fail_at=PART * 7)

    async def test_short_download_is_error_not_silent(self):
        with self.assertRaises(IOError):
            await self.run_dl(PART * 12, 2, short=True)

    def test_plan_and_env(self):
        self.assertEqual(fd.plan(10 * PART, 4, PART), [(0, 3), (3, 3), (6, 3), (9, 1)])
        self.assertEqual(fd.connections_from_env({"DOWNLOAD_CONNECTIONS": "99"}), 8)
        self.assertEqual(fd.connections_from_env({"DOWNLOAD_CONNECTIONS": "x"}), 4)
        self.assertEqual(fd.connections_from_env({"DOWNLOAD_CONNECTIONS": "1"}), 1)


if __name__ == "__main__":
    unittest.main()
