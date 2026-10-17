"""Narrow, read-only Twikit reader for explicitly configured public X accounts."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as dt
import importlib
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

REQUEST_TIMEOUT_SECONDS = 12
OVERALL_TIMEOUT_SECONDS = 120
MAX_ACCOUNTS = 8
MAX_POSTS_PER_ACCOUNT = 20
MAX_TEXT = 700
DEFAULT_INTER_ACCOUNT_DELAY = 1.5
_KEY_BYTE_ERROR = "Couldn't get KEY_BYTE indices"


class XReaderError(RuntimeError):
    """Safe, credential-free failure description."""


def _fail(context: str, exc: BaseException | str) -> XReaderError:
    name = exc if isinstance(exc, str) else type(exc).__name__
    return XReaderError(f"{context}: {name}")


def _account_handles(source: dict[str, Any]) -> list[str]:
    accounts = source.get("accounts") if isinstance(source, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise XReaderError("accounts: ConfigurationError")
    if len(accounts) > MAX_ACCOUNTS:
        raise XReaderError("accounts: AccountLimitExceeded")
    result, seen = [], set()
    for value in accounts:
        handle = str(value).strip().removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise XReaderError("accounts: InvalidAccount")
        normalized = handle.lower()
        if normalized not in seen:
            result.append(handle)
            seen.add(normalized)
    return result


def _cookies_from_environment() -> dict[str, Any]:
    encoded = os.environ.get("X_READER_COOKIES_JSON")
    cookie_file = os.environ.get("X_READER_COOKIES_FILE")
    if encoded:
        try:
            cookies = json.loads(encoded)
        except Exception as exc:
            raise _fail("credentials", "InvalidCookieJSON") from None
    elif cookie_file:
        try:
            with Path(cookie_file).open(encoding="utf-8") as stream:
                cookies = json.load(stream)
        except Exception as exc:
            raise _fail("credentials", "CookieFileUnavailable") from None
    else:
        raise XReaderError("credentials: MissingCredentials")
    if (not isinstance(cookies, dict) or not cookies
            or not isinstance(cookies.get("auth_token"), str) or not cookies["auth_token"].strip()
            or not isinstance(cookies.get("ct0"), str) or not cookies["ct0"].strip()):
        raise XReaderError("credentials: MissingRequiredCookies")
    # Pass only credentials the read-only session needs; discard optional prefs.
    return {"auth_token": cookies["auth_token"], "ct0": cookies["ct0"]}


def _parse_published(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = dt.datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %z %Y")
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is None:
        # X posts must carry a real timestamp; an ambiguous naive date is not publishable.
        return None
    return parsed.astimezone(dt.timezone.utc)


def _get(value: Any, *names: str, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


_MISSING = object()


def _nested(value: Any, *path: str, default=None):
    current = value
    for key in path:
        current = _get(current, key, default=_MISSING)
        if current is _MISSING:
            return default
    return current


def _public_profile(user: Any) -> tuple[str, str]:
    # Missing privacy status is not proof that an account is public.
    protected = _get(user, "protected", default=_MISSING)
    if protected is _MISSING or protected is None:
        protected = _nested(user, "privacy", "protected", default=_MISSING)
        if protected is _MISSING:
            protected = _nested(user, "legacy", "protected", default=_MISSING)
    if protected is _MISSING:
        protected = _get(user, "is_protected", default=None)
    if protected is not False:
        raise XReaderError("account: ProtectedOrUnknownAccount")
    screen_name = _get(user, "screen_name", "username", default="")
    user_id = _get(user, "id", "id_str", "rest_id", default="")
    if not screen_name:
        screen_name = (_nested(user, "core", "screen_name", default="")
                       or _nested(user, "legacy", "screen_name", default=""))
    if not user_id:
        user_id = _nested(user, "legacy", "id_str", default="") or _nested(user, "legacy", "id", default="")
    if not isinstance(screen_name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", screen_name):
        raise XReaderError("account: InvalidReturnedAuthor")
    if not re.fullmatch(r"[0-9]+", str(user_id)):
        raise XReaderError("account: InvalidReturnedAuthor")
    return screen_name, str(user_id)


def _post_record(post: Any, requested_user: Any, handle: str) -> dict[str, Any] | None:
    author = _get(post, "user", "author", default=None) or requested_user
    screen_name, _ = _public_profile(author)
    expected_screen = _get(requested_user, "screen_name", "username", default="")
    if screen_name.casefold() != str(expected_screen or handle).casefold():
        return None
    post_id = _get(post, "id", "id_str", "rest_id", default="")
    if not re.fullmatch(r"[0-9]+", str(post_id)):
        return None
    text = _get(post, "text", "full_text", "rawContent", default="")
    if not isinstance(text, str):
        text = str(text or "")
    published = _parse_published(_get(post, "created_at_datetime", "created_at", "createdAt", default=None))
    return {"title": text[:MAX_TEXT], "excerpt": text[:MAX_TEXT],
            "url": f"https://x.com/{screen_name}/status/{post_id}", "published": published}


def _set_request_timeout(client: Any, timeout: float) -> None:
    """Best-effort set transport timeout and bound common async request methods."""
    setattr(client, "request_timeout", timeout)
    http = _get(client, "http", "_http", "http_client", default=None)
    if http is None:
        return
    for attr in ("timeout", "request_timeout"):
        if hasattr(http, attr):
            try:
                setattr(http, attr, timeout)
            except Exception:
                pass
    for method_name in ("request", "get", "post"):
        original = getattr(http, method_name, None)
        if not callable(original) or getattr(original, "_x_reader_bounded", False):
            continue

        async def bounded(*args, __original=original, **kwargs):
            result = __original(*args, **kwargs)
            if inspect.isawaitable(result):
                return await asyncio.wait_for(result, timeout=timeout)
            return result

        bounded._x_reader_bounded = True
        try:
            setattr(http, method_name, bounded)
        except Exception:
            pass


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _compat_patch(transaction_cls, user_cls):
    """Temporary, narrowly scoped compatibility wrappers; restores both classes."""
    @contextlib.contextmanager
    def patch():
        original_init = getattr(transaction_cls, "init")
        original_generate = getattr(transaction_cls, "generate_transaction_id")
        original_user_init = getattr(user_cls, "__init__")

        def init_wrapper_factory(original):
            if inspect.iscoroutinefunction(original):
                async def wrapped(self, *args, **kwargs):
                    try:
                        return await original(self, *args, **kwargs)
                    except Exception as exc:
                        if str(exc) != _KEY_BYTE_ERROR:
                            raise
                        self._news_unsigned = True
                        return None
            else:
                def wrapped(self, *args, **kwargs):
                    try:
                        return original(self, *args, **kwargs)
                    except Exception as exc:
                        if str(exc) != _KEY_BYTE_ERROR:
                            raise
                        self._news_unsigned = True
                        return None
            return wrapped

        def generate_wrapper(self, *args, **kwargs):
            if getattr(self, "_news_unsigned", False):
                return ""
            return original_generate(self, *args, **kwargs)

        defaults = {
            "verified": False, "default_profile": False, "default_profile_image": False,
            "is_translator": False, "is_translation_enabled": False,
            "is_profile_translatable": False, "can_dm": False, "can_media_tag": False,
            "followers_count": 0, "friends_count": 0, "listed_count": 0,
            "statuses_count": 0, "favourites_count": 0, "media_count": 0,
            "pinned_tweet_ids": [], "withheld_in_countries": [],
            "entities": {}, "description_entities": {}, "url_entities": {},
            # Missing privacy metadata must fail closed at the account guard.
            "protected": True,
        }

        def user_init(self, *args, **kwargs):
            args = list(args)
            kwargs = dict(kwargs)

            def normalize(data):
                data = copy.deepcopy(data)
                # Carry forward only identity/privacy fields explicitly present in the
                # new nested response shape; never synthesize identity or public status.
                core = data.get("core") if isinstance(data.get("core"), dict) else {}
                legacy = data.get("legacy") if isinstance(data.get("legacy"), dict) else {}
                privacy = data.get("privacy") if isinstance(data.get("privacy"), dict) else {}
                screen = data.get("screen_name") or core.get("screen_name") or legacy.get("screen_name")
                name = data.get("name") or core.get("name") or legacy.get("name")
                if screen and not data.get("screen_name"):
                    data["screen_name"] = screen
                if name and not data.get("name"):
                    data["name"] = name
                identity = data.get("id") or data.get("id_str") or data.get("rest_id") or legacy.get("id_str") or legacy.get("id")
                if identity and not data.get("id"):
                    data["id"] = identity
                if not isinstance(data.get("protected"), bool):
                    if isinstance(privacy.get("protected"), bool):
                        data["protected"] = privacy["protected"]
                    elif isinstance(legacy.get("protected"), bool):
                        data["protected"] = legacy["protected"]
                    else:
                        data["protected"] = True
                for key, value in defaults.items():
                    if key == "protected":
                        continue
                    data.setdefault(key, copy.deepcopy(value))
                # Twikit 2.3.3 still reads profile metadata from legacy, while X
                # moved some real identity/privacy fields into core/privacy.
                data["legacy"] = legacy
                if identity:
                    data.setdefault("rest_id", str(identity))
                if screen:
                    legacy.setdefault("screen_name", screen)
                if name:
                    legacy.setdefault("name", name)
                legacy["protected"] = data["protected"]
                legacy_defaults = dict(defaults, created_at="", name="", location="",
                    description="", profile_image_url_https="", pinned_tweet_ids_str=[],
                    possibly_sensitive=False, want_retweets=False, has_custom_timelines=False,
                    fast_followers_count=0, normal_followers_count=0, translator_type="")
                for key, value in legacy_defaults.items():
                    legacy.setdefault(key, copy.deepcopy(value))
                entities = legacy.setdefault("entities", {})
                entities.setdefault("description", {}).setdefault("urls", [])
                entities.setdefault("url", {}).setdefault("urls", [])
                data.setdefault("is_blue_verified", False)
                return data

            for pos in range(1, len(args)):
                if isinstance(args[pos], dict):
                    args[pos] = normalize(args[pos])
                    break
            else:
                for key, value in list(kwargs.items()):
                    if isinstance(value, dict):
                        kwargs[key] = normalize(value)
                        break
            return original_user_init(self, *args, **kwargs)

        transaction_cls.init = init_wrapper_factory(original_init)
        transaction_cls.generate_transaction_id = generate_wrapper
        user_cls.__init__ = user_init
        try:
            yield
        finally:
            transaction_cls.init = original_init
            transaction_cls.generate_transaction_id = original_generate
            user_cls.__init__ = original_user_init
    return patch()


def _load_twikit():
    try:
        twikit = importlib.import_module("twikit")
        transaction_module = importlib.import_module("twikit.x_client_transaction.transaction")
        user_module = importlib.import_module("twikit.user")
        return twikit, transaction_module.ClientTransaction, user_module.User
    except Exception as exc:
        raise _fail("twikit", "ImportError") from None


def _default_client_factory():
    twikit, _, _ = _load_twikit()
    return twikit.Client(language="en-US")


async def _close_client(client: Any) -> None:
    http = _get(client, "http", "_http", "http_client", default=None)
    close = (getattr(client, "aclose", None) or getattr(client, "close", None)
             or getattr(http, "aclose", None) or getattr(http, "close", None))
    if callable(close):
        await _maybe_await(close())


async def _fetch_async(source: dict[str, Any], *, client_factory, compat: bool,
                       sleep: Callable, delay: float) -> list[dict[str, Any]]:
    handles = _account_handles(source)
    cookies = _cookies_from_environment()
    transaction_cls = user_cls = None
    if compat:
        _, transaction_cls, user_cls = _load_twikit()
    context = _compat_patch(transaction_cls, user_cls) if compat else contextlib.nullcontext()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    with context:
        client = None
        try:
            try:
                client = client_factory()
                _set_request_timeout(client, REQUEST_TIMEOUT_SECONDS)
                setter = getattr(client, "set_cookies", None)
                if callable(setter):
                    await _maybe_await(setter(cookies))
                else:
                    jar = getattr(client, "cookies", None)
                    if jar is None or not callable(getattr(jar, "update", None)):
                        raise XReaderError("client: CookieSetupUnavailable")
                    jar.update(cookies)
            except XReaderError:
                raise
            except Exception as exc:
                raise _fail("client", exc) from None
            for index, handle in enumerate(handles):
                try:
                    user = await _maybe_await(client.get_user_by_screen_name(handle))
                    screen_name, user_id = _public_profile(user)
                    if screen_name.casefold() != handle.casefold():
                        raise XReaderError("account: ReturnedAccountMismatch")
                    posts = await _maybe_await(client.get_user_tweets(user_id, "Tweets", count=MAX_POSTS_PER_ACCOUNT))
                    for post in list(posts or [])[:MAX_POSTS_PER_ACCOUNT]:
                        record = _post_record(post, user, handle)
                        if record and record["url"] not in seen:
                            result.append(record)
                            seen.add(record["url"])
                except XReaderError as exc:
                    reason = str(exc).split(": ", 1)[-1]
                    reason = re.sub(r"[^A-Za-z0-9_]+", "", reason) or "ReaderError"
                    raise XReaderError(f"{handle}: {reason}") from None
                except Exception as exc:
                    raise _fail(handle, exc) from None
                if delay > 0 and index + 1 < len(handles):
                    await _maybe_await(sleep(delay))
        finally:
            if client is not None:
                try:
                    await _close_client(client)
                except Exception:
                    raise _fail("client", "CloseError") from None
    return result


def fetch_posts(source: dict[str, Any], *, client_factory=None, compat: bool = True,
                sleep: Callable = asyncio.sleep,
                inter_account_delay: float = DEFAULT_INTER_ACCOUNT_DELAY,
                overall_timeout: float = OVERALL_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Fetch at most 20 public posts for each of up to eight configured accounts."""
    factory = client_factory or _default_client_factory

    async def bounded():
        return await asyncio.wait_for(
            _fetch_async(source, client_factory=factory, compat=compat, sleep=sleep,
                         delay=inter_account_delay), timeout=overall_timeout)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise XReaderError("reader: RunningEventLoop")
    try:
        return asyncio.run(bounded())
    except XReaderError:
        raise
    except asyncio.TimeoutError:
        raise XReaderError("reader: TimeoutError") from None
    except Exception as exc:
        raise _fail("reader", exc) from None


