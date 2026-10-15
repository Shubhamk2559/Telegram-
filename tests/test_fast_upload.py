import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import fast_upload as fu
from src.diskwala_uploader import TelethonBotChannel

PART = 1024


class FakeTL:
    class SaveBigFilePartRequest:
        def __init__(self, file_id, file_part, file_total_parts, bytes):
            self.file_id, self.file_part, self.total, self.bytes = file_id, file_part, file_total_parts, bytes

    class InputFileBig:
        def __init__(self, id, parts, name):
            self.id, self.parts, self.name = id, parts, name

    class InputMediaUploadedDocument:
        def __init__(self, file, mime_type, attributes, force_file=False):
            self.file, self.mime_type, self.attributes, self.force_file = file, mime_type, attributes, force_file

    class DocumentAttributeFilename:
        def __init__(self, file_name):
            self.file_name = file_name


class FakeClient:
    def __init__(self, fail_part=None, flaky_part=None, send_error=None):
        self.parts, self.inflight, self.max_inflight = {}, 0, 0
        self.fail_part, self.flaky_part, self.send_error = fail_part, flaky_part, send_error
        self.flaked = False
        self.sent, self.send_file_calls = [], []

    async def __call__(self, req):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0.001)
            if req.file_part == self.fail_part:
                raise RuntimeError("boom")
            if req.file_part == self.flaky_part and not self.flaked:
                self.flaked = True
                raise ConnectionError("reset")
            self.parts[req.file_part] = req.bytes
            return True
        finally:
            self.inflight -= 1

    async def send_file(self, entity, file, **kw):
        self.send_file_calls.append((file, kw))
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(id=777)


class Parallel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "vid.mp4"
        self.data = os.urandom(PART * 10 + 77)
        self.path.write_bytes(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_all_parts_uploaded_in_parallel(self):
        c, prog = FakeClient(), []
        h = await fu.parallel_upload_file(c, self.path, 4, lambda d, t: prog.append((d, t)), part=PART, tl=FakeTL)
        self.assertEqual(h.parts, 11)
        self.assertEqual(h.name, "vid.mp4")
        self.assertEqual(b"".join(c.parts[i] for i in range(11)), self.data)
        self.assertGreater(c.max_inflight, 1)
        self.assertLessEqual(c.max_inflight, 4)
        self.assertEqual(prog[-1], (len(self.data), len(self.data)))

    async def test_part_error_propagates(self):
        with self.assertRaises(RuntimeError):
            await fu.parallel_upload_file(FakeClient(fail_part=5), self.path, 4, part=PART, tl=FakeTL)

    async def test_transient_part_error_is_retried(self):
        c = FakeClient(flaky_part=3)
        orig = asyncio.sleep
        asyncio.sleep = lambda *_a, **_k: orig(0)
        try:
            h = await fu.parallel_upload_file(c, self.path, 3, part=PART, tl=FakeTL)
        finally:
            asyncio.sleep = orig
        self.assertEqual(h.parts, 11)
        self.assertEqual(len(c.parts), 11)

    async def test_send_as_document_with_filename(self):
        c = FakeClient()
        fu_part = fu.PART
        fu.PART = PART
        try:
            orig = fu.parallel_upload_file

            async def small(client, path, n, cb, tl=None, part=PART):
                return await orig(client, path, n, cb, part=PART, tl=tl)
            fu.parallel_upload_file = small
            msg = await fu.send_file_parallel(c, "ent", str(self.path), None, 4, tl=FakeTL)
        finally:
            fu.parallel_upload_file = orig
            fu.PART = fu_part
        self.assertEqual(msg.id, 777)
        media, kw = c.send_file_calls[0]
        self.assertTrue(media.force_file)
        self.assertEqual(media.attributes[0].file_name, "vid.mp4")
        self.assertEqual(media.mime_type, "video/mp4")

    async def test_send_phase_errors_are_classified(self):
        for err, exc in ((TypeError("bad media"), fu.UploadNotSent), (ConnectionError("x"), fu.SendPhaseError)):
            c = FakeClient(send_error=err)
            with self.assertRaises(exc):
                await fu.send_file_parallel(c, "ent", str(self.path), None, 2, tl=FakeTL)

    def test_env(self):
        self.assertEqual(fu.connections_from_env({}), 8)
        self.assertEqual(fu.connections_from_env({"UPLOAD_CONNECTIONS": "99"}), 16)
        self.assertEqual(fu.connections_from_env({"UPLOAD_CONNECTIONS": "1"}), 1)
        self.assertEqual(fu.connections_from_env({"UPLOAD_CONNECTIONS": "zz"}), 8)


class ChannelFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "big.mp4"
        self.path.write_bytes(b"x" * 2048)
        self._min, self._tl = fu.MIN_SIZE, fu._tl
        fu.MIN_SIZE = 1024
        fu._tl = lambda: FakeTL

    def tearDown(self):
        fu.MIN_SIZE, fu._tl = self._min, self._tl
        self.tmp.cleanup()

    async def test_upload_phase_failure_falls_back_to_normal_send(self):
        c = FakeClient(fail_part=0)
        ch = TelethonBotChannel(c, "ent", as_document=True)
        self.assertEqual(await ch.send_video(self.path, lambda d, t: None), 777)
        self.assertEqual(len(c.send_file_calls), 1)
        self.assertEqual(c.send_file_calls[0][0], str(self.path))   # normal path-based send_file

    async def test_send_phase_error_is_not_resent(self):
        c = FakeClient(send_error=ConnectionError("reset"))
        ch = TelethonBotChannel(c, "ent", as_document=True)
        with self.assertRaises(ConnectionError):
            await ch.send_video(self.path, lambda d, t: None)
        self.assertEqual(len(c.send_file_calls), 1)                 # sirf parallel wali koshish, dobara nahi

    async def test_video_mode_and_small_files_use_normal_send(self):
        c = FakeClient()
        await TelethonBotChannel(c, "ent", as_document=False).send_video(self.path, None)
        self.assertEqual(c.send_file_calls[0][0], str(self.path))
        fu.MIN_SIZE = 10**9
        c2 = FakeClient()
        await TelethonBotChannel(c2, "ent", as_document=True).send_video(self.path, None)
        self.assertEqual(c2.send_file_calls[0][0], str(self.path))


if __name__ == "__main__":
    unittest.main()
