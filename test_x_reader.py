import asyncio
import datetime as dt
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import x_reader


COOKIES = {"auth_token": "TEST_AUTH_SECRET", "ct0": "TEST_CSRF_SECRET", "lang": "fr"}


class FakeClient:
    def __init__(self, users=None, posts=None, errors=None):
        self.users = users or {}
        self.posts = posts or {}
        self.errors = errors or {}
        self.cookie_data = None
        self.calls = []
        self.closed = False
        self.http = SimpleNamespace(timeout=None, request_timeout=None)

    def set_cookies(self, cookies):
        self.cookie_data = cookies

    async def get_user_by_screen_name(self, handle):
        self.calls.append(("lookup", handle))
        if handle in self.errors:
            raise self.errors[handle]
        return self.users[handle]

    async def get_user_tweets(self, user_id, mode, count):
        self.calls.append(("tweets", user_id, mode, count))
        return self.posts.get(user_id, [])

    async def aclose(self):
        self.closed = True


def public_user(handle="alice", user_id="100"):
    return SimpleNamespace(screen_name=handle, id=user_id, protected=False)


def post(post_id="500", text="A public post", created_at="2026-10-17T08:00:00Z", author=None):
    return SimpleNamespace(id=post_id, text=text, created_at=created_at, user=author)


class XReaderTests(unittest.TestCase):
    def run_reader(self, source, client, **kwargs):
        with patch.dict(os.environ, {"X_READER_COOKIES_JSON": json.dumps(COOKIES)}, clear=False):
            os.environ.pop("X_READER_COOKIES_FILE", None)
            kwargs.setdefault("inter_account_delay", 0)
            return x_reader.fetch_posts(source, client_factory=lambda: client, compat=False, **kwargs)

    def test_success_limits_posts_sanitizes_metadata_and_closes_client(self):
        author = public_user("alice", "100")
        long_text = "x" * 900
        client = FakeClient(users={"alice": author}, posts={"100": [
            post(str(i), long_text, author=author) for i in range(1, 23)
        ]})
        rows = self.run_reader({"accounts": ["alice"]}, client)
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["url"], "https://x.com/alice/status/1")
        self.assertEqual(rows[0]["title"], "x" * 700)
        self.assertEqual(rows[0]["excerpt"], "x" * 700)
        self.assertEqual(rows[0]["published"], dt.datetime(2026, 10, 17, 8, tzinfo=dt.timezone.utc))
        self.assertNotIn("auth_token", repr(rows))
        self.assertNotIn("ct0", repr(rows))
        self.assertEqual(client.cookie_data, {"auth_token": COOKIES["auth_token"], "ct0": COOKIES["ct0"]})
        self.assertEqual(client.calls[-1], ("tweets", "100", "Tweets", 20))
        self.assertTrue(client.closed)
        self.assertEqual(client.request_timeout, x_reader.REQUEST_TIMEOUT_SECONDS)

    def test_accounts_are_sequential_and_delay_is_injected(self):
        one, two = public_user("alice", "100"), public_user("bob", "200")
        client = FakeClient(users={"alice": one, "bob": two}, posts={
            "100": [post("1", author=one)], "200": [post("2", author=two)]})
        waits = []
        async def fake_sleep(delay):
            waits.append(delay)
        rows = self.run_reader({"accounts": ["alice", "bob"]}, client,
                               sleep=fake_sleep, inter_account_delay=1.5)
        self.assertEqual(waits, [1.5])
        self.assertEqual([r["url"] for r in rows], [
            "https://x.com/alice/status/1", "https://x.com/bob/status/2"])
        self.assertTrue(client.closed)

    def test_private_or_unknown_privacy_status_is_rejected_and_closed(self):
        for user in (SimpleNamespace(screen_name="alice", id="100", protected=True),
                     SimpleNamespace(screen_name="alice", id="100")):
            with self.subTest(protected=getattr(user, "protected", None)):
                client = FakeClient(users={"alice": user})
                with self.assertRaises(x_reader.XReaderError):
                    self.run_reader({"accounts": ["alice"]}, client)
                self.assertEqual(client.calls, [("lookup", "alice")])
                self.assertTrue(client.closed)

    def test_nested_core_identity_and_privacy_are_used_without_guessing(self):
        profile = {"rest_id": "123", "core": {"name": "Alice", "screen_name": "alice"},
                   "privacy": {"protected": False}}
        self.assertEqual(x_reader._public_profile(profile), ("alice", "123"))
        profile["privacy"]["protected"] = True
        with self.assertRaisesRegex(x_reader.XReaderError, "ProtectedOrUnknownAccount"):
            x_reader._public_profile(profile)

    def test_invalid_or_mismatched_post_authors_and_ids_are_never_linked(self):
        alice, bob = public_user("alice", "100"), public_user("bob", "200")
        client = FakeClient(users={"alice": alice}, posts={"100": [
            post("not-numeric", author=alice), post("502", author=bob), post("503", author=alice)
        ]})
        rows = self.run_reader({"accounts": ["alice"]}, client)
        self.assertEqual([r["url"] for r in rows], ["https://x.com/alice/status/503"])

    def test_missing_or_ambiguous_date_is_none_without_today_fallback(self):
        author = public_user()
        client = FakeClient(users={"alice": author}, posts={"100": [
            post("1", created_at=None, author=author),
            post("2", created_at="not a date", author=author),
            post("3", created_at=dt.datetime(2026, 10, 17, 10), author=author),
        ]})
        rows = self.run_reader({"accounts": ["alice"]}, client)
        self.assertEqual([row["published"] for row in rows], [None, None, None])

    def test_created_at_datetime_is_preferred_when_present(self):
        author = public_user()
        tweet = post("7", author=author)
        tweet.created_at_datetime = dt.datetime(2026, 10, 16, 7, tzinfo=dt.timezone.utc)
        tweet.created_at = "invalid"
        client = FakeClient(users={"alice": author}, posts={"100": [tweet]})
        rows = self.run_reader({"accounts": ["alice"]}, client)
        self.assertEqual(rows[0]["published"], dt.datetime(2026, 10, 16, 7, tzinfo=dt.timezone.utc))

    def test_missing_credentials_and_cookie_parse_errors_never_echo_secret(self):
        client = FakeClient()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(x_reader.XReaderError, "MissingCredentials"):
                x_reader.fetch_posts({"accounts": ["alice"]}, client_factory=lambda: client, compat=False)
        with patch.dict(os.environ, {"X_READER_COOKIES_JSON": "not-json-SECRET"}, clear=True):
            with self.assertRaises(x_reader.XReaderError) as caught:
                x_reader.fetch_posts({"accounts": ["alice"]}, client_factory=lambda: client, compat=False)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_api_error_is_sanitized_and_client_is_closed(self):
        client = FakeClient(errors={"alice": RuntimeError("auth_token=TEST_AUTH_SECRET")})
        with self.assertRaises(x_reader.XReaderError) as caught:
            self.run_reader({"accounts": ["alice"]}, client)
        self.assertEqual(str(caught.exception), "alice: RuntimeError")
        self.assertNotIn("TEST_AUTH_SECRET", str(caught.exception))
        self.assertTrue(client.closed)

    def test_account_count_and_handle_validation(self):
        client = FakeClient()
        with self.assertRaisesRegex(x_reader.XReaderError, "AccountLimitExceeded"):
            self.run_reader({"accounts": [f"user{i}" for i in range(9)]}, client)
        with self.assertRaisesRegex(x_reader.XReaderError, "InvalidAccount"):
            self.run_reader({"accounts": ["alice; secret"]}, client)

    def test_request_methods_are_bounded_by_transport_timeout(self):
        class SlowHTTP:
            async def get(self, url):
                await asyncio.sleep(0.1)
                return url
        http = SlowHTTP()
        client = SimpleNamespace(http=http)
        x_reader._set_request_timeout(client, 0.01)
        self.assertEqual(client.request_timeout, 0.01)
        async def run():
            with self.assertRaises(asyncio.TimeoutError):
                await http.get("https://example.test")
        asyncio.run(run())

    def test_overall_timeout_still_closes_client(self):
        class SlowClient(FakeClient):
            async def get_user_by_screen_name(self, handle):
                await asyncio.sleep(1)
        client = SlowClient(users={"alice": public_user()})
        with self.assertRaisesRegex(x_reader.XReaderError, "TimeoutError"):
            self.run_reader({"accounts": ["alice"]}, client, overall_timeout=0.01)
        self.assertTrue(client.closed)

    def test_compat_patch_only_handles_exact_transaction_error_and_restores_classes(self):
        class Transaction:
            async def init(self):
                raise Exception("Couldn't get KEY_BYTE indices")
            def generate_transaction_id(self):
                return "original"
        class User:
            def __init__(self, client, data):
                self.data = data
        original_init, original_generate, original_user_init = Transaction.init, Transaction.generate_transaction_id, User.__init__
        with x_reader._compat_patch(Transaction, User):
            tx = Transaction()
            asyncio.run(tx.init())
            self.assertEqual(tx.generate_transaction_id(), "")
            ordinary = Transaction()
            self.assertEqual(ordinary.generate_transaction_id(), "original")
            input_data = {"id": "100", "screen_name": "alice", "protected": False}
            user = User(None, input_data)
            self.assertEqual(user.data["id"], "100")
            self.assertEqual(user.data["screen_name"], "alice")
            self.assertFalse(user.data["protected"])
            self.assertEqual(user.data["followers_count"], 0)
            self.assertEqual(input_data, {"id": "100", "screen_name": "alice", "protected": False})
            nested_input = {"rest_id": "200", "core": {"name": "Alice Example", "screen_name": "alice"},
                            "privacy": {"protected": False}}
            nested_user = User(None, nested_input)
            self.assertEqual(nested_user.data["id"], "200")
            self.assertEqual(nested_user.data["name"], "Alice Example")
            self.assertEqual(nested_user.data["screen_name"], "alice")
            self.assertFalse(nested_user.data["protected"])
            self.assertEqual(nested_input, {"rest_id": "200", "core": {"name": "Alice Example", "screen_name": "alice"},
                                            "privacy": {"protected": False}})
            legacy_user = User(None, {"rest_id": "201", "core": {"screen_name": "bob"},
                                      "legacy": {"protected": False}})
            self.assertFalse(legacy_user.data["protected"])
            unknown = User(None, {"rest_id": "202", "core": {"screen_name": "carol"}})
            self.assertTrue(unknown.data["protected"])
            with self.assertRaisesRegex(Exception, "other transaction error"):
                class OtherTransaction(Transaction):
                    async def init(self):
                        raise Exception("other transaction error")
                asyncio.run(OtherTransaction().init())
        self.assertIs(Transaction.init, original_init)
        self.assertIs(Transaction.generate_transaction_id, original_generate)
        self.assertIs(User.__init__, original_user_init)

    def test_compatibility_preserves_real_twikit_legacy_constructor_contract(self):
        class Transaction:
            async def init(self): pass
            def generate_transaction_id(self): return 'original'
        class LegacyUser:
            def __init__(self, client, data):
                legacy = data['legacy']
                self.id, self.screen_name = data['rest_id'], legacy['screen_name']
                self.name, self.protected = legacy['name'], legacy['protected']
                self.profile_image = legacy['profile_image_url_https']
                self.urls = legacy['entities']['description']['urls']
                self.pinned = legacy['pinned_tweet_ids_str']
                self.blue = data['is_blue_verified']
        with x_reader._compat_patch(Transaction, LegacyUser):
            u = LegacyUser(None, {'rest_id':'42', 'core':{'screen_name':'alice','name':'Alice'},
                                  'privacy':{'protected':False}, 'legacy':{}})
        self.assertEqual((u.id, u.screen_name, u.name, u.protected), ('42','alice','Alice',False))


if __name__ == "__main__":
    unittest.main()
