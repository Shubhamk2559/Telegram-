import unittest

from src.diskwala_uploader import (
    BotReply, extract_urls, looks_bot_error, looks_manual, looks_rate_limited, normalize_url, reply_from_message,
)


class ParserTests(unittest.TestCase):
    def test_plain_text_link_and_trailing_punctuation(self):
        self.assertEqual(extract_urls("Your link: https://dw.example/abc123."), ["https://dw.example/abc123"])
        self.assertEqual(extract_urls("(https://dw.example/x)"), ["https://dw.example/x"])

    def test_multiple_links_dedup_order(self):
        t = "Stream https://a.com/s Download https://b.com/d again https://a.com/s"
        self.assertEqual(extract_urls(t), ["https://a.com/s", "https://b.com/d"])

    def test_hidden_entity_and_button_links(self):
        self.assertEqual(
            extract_urls("Open here", entity_urls=["https://hidden.io/z"], button_urls=["https://btn.io/q"]),
            ["https://hidden.io/z", "https://btn.io/q"])

    def test_www_gets_https(self):
        self.assertEqual(extract_urls("see www.site.com/f1"), ["https://www.site.com/f1"])

    def test_no_link(self):
        self.assertEqual(extract_urls("Processing your file..."), [])
        self.assertEqual(extract_urls(""), [])
        self.assertIsNone(normalize_url("http://localhost"))
        self.assertIsNone(normalize_url("ftp://a.com/x"))

    def test_domain_filter(self):
        t = "Join https://t.me/promo and get https://files.dw.com/v/9"
        self.assertEqual(extract_urls(t, domains=("dw.com",)), ["https://files.dw.com/v/9"])
        self.assertEqual(extract_urls(t, domains=("nothere.com",)), [])

    def test_detectors(self):
        self.assertTrue(looks_manual("Please solve the CAPTCHA"))
        self.assertTrue(looks_manual("You must join our channel first"))
        self.assertTrue(looks_manual("Send /start to begin"))
        self.assertFalse(looks_manual("Verifying your file... processing"))
        self.assertTrue(looks_rate_limited("Daily limit reached"))
        self.assertTrue(looks_rate_limited("Too many requests, try again later"))
        self.assertFalse(looks_rate_limited("Please wait, processing"))
        self.assertTrue(looks_bot_error("Error: unsupported format"))
        self.assertFalse(looks_bot_error("Uploading 50%"))

    def test_reply_from_message_uses_caption_entities_buttons(self):
        class E: url = "https://hid.den/1"
        class B: url = "https://btn.io/2"
        class NoUrl: url = None
        class M:
            id = 7; raw_text = "Done! Caption text"; entities = [E(), object()]
            buttons = [[B(), NoUrl()]]; reply_to_msg_id = 5
        r = reply_from_message(M())
        self.assertEqual((r.id, r.reply_to_id), (7, 5))
        self.assertEqual(extract_urls(r.text, r.entity_urls, r.button_urls), ["https://hid.den/1", "https://btn.io/2"])


if __name__ == "__main__":
    unittest.main()
