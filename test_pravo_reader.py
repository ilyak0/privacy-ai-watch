import datetime as dt
import json
import unittest
from urllib.parse import parse_qs, urlparse

import pravo_reader as reader


def page(items, *, current=1, pages=1, total=None):
    return {
        "items": items,
        "itemsTotalCount": len(items) if total is None else total,
        "itemsPerPage": 200,
        "currentPage": current,
        "pagesTotalCount": pages,
    }


def act(number, title, date="17.10.2026", **extra):
    return {"eoNumber": str(number), "title": title,
            "publishDateShort": date, **extra}


class PravoReaderTests(unittest.TestCase):
    def test_real_api_iso_publication_date_and_network_timeout(self):
        def fake_fetch(_):
            return json.dumps(page([act("2001202610020002", "AI", date="2026-10-02T00:00:00")])).encode(), "application/json"
        rows, _ = reader.fetch_documents({}, 30, dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc), fake_fetch)
        self.assertEqual(rows[0]["published"], dt.datetime(2026, 10, 1, 21, tzinfo=dt.timezone.utc))
        def timeout(_):
            raise TimeoutError("network timed out")
        with self.assertRaisesRegex(TimeoutError, "network timed out"):
            reader.fetch_documents({}, 30, dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc), timeout)

    def test_queries_dates_timezone_and_id_dedup(self):
        calls = []
        payloads = [
            page([act("2001202610020002", "Указ об искусственном интеллекте")]),
            page([act("2001202610020002", "Указ об искусственном интеллекте"),
                  act("2001202610030003", "О персональных данных")]),
        ]

        def fake_fetch(url):
            calls.append(url)
            return json.dumps(payloads[len(calls) - 1], ensure_ascii=False).encode(), "application/json"

        now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
        items, meta = reader.fetch_documents(
            {"title_queries": ["искусственного интеллекта", "персональных данных"]},
            30, now, fake_fetch)

        self.assertEqual(len(calls), 2)
        params = parse_qs(urlparse(calls[0]).query)
        self.assertEqual(params["Name"], ["искусственного интеллекта"])
        self.assertEqual(params["PublishDateSearchType"], ["0"])
        self.assertEqual(params["PublishDateFrom"], ["17.09.2026"])
        self.assertEqual(params["PublishDateTo"], ["17.10.2026"])
        self.assertEqual(params["PageSize"], ["200"])
        self.assertEqual(params["Index"], ["1"])
        self.assertEqual(len(items), 2)
        ai_item = next(item for item in items if "искусственном" in item["title"])
        self.assertEqual(ai_item["url"],
                         "http://publication.pravo.gov.ru/document/2001202610020002")
        self.assertEqual(ai_item["published"],
                         dt.datetime(2026, 10, 16, 21, tzinfo=dt.timezone.utc))
        self.assertEqual(ai_item["excerpt"], "")
        self.assertEqual(meta["window_coverage"], "boundary_reached")
        self.assertEqual(meta["pages_fetched"], 2)
        self.assertGreater(meta["bytes_received"], 0)

    def test_per_query_page_cap_is_visible_and_keeps_results(self):
        calls = []

        def fake_fetch(url):
            page_number = int(parse_qs(urlparse(url).query)["Index"][0])
            calls.append(page_number)
            body = page([act(f"2001202610{page_number:02d}00001", f"Акт {page_number}")],
                        current=page_number, pages=3, total=3)
            return json.dumps(body, ensure_ascii=False).encode(), "application/json"

        items, meta = reader.fetch_documents(
            {"title_queries": ["персональных данных"]}, 30,
            dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc), fake_fetch)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(items), 2)
        self.assertEqual(meta["window_coverage"], "capped")
        self.assertEqual(meta["capped_queries"], ["персональных данных"])

    def test_total_request_cap_does_not_drop_prior_query_results(self):
        calls = []

        def fake_fetch(url):
            params = parse_qs(urlparse(url).query)
            page_number = int(params["Index"][0])
            query = params["Name"][0]
            calls.append((query, page_number))
            n = len(calls)
            number = str(2001202600000000 + n)
            body = page([act(number, f"Акт {n}")], current=page_number,
                        pages=2, total=2)
            return json.dumps(body, ensure_ascii=False).encode(), "application/json"

        queries = [f"запрос {i}" for i in range(6)]
        items, meta = reader.fetch_documents(
            {"title_queries": queries}, 30,
            dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc), fake_fetch)
        self.assertEqual(len(calls), reader.MAX_TOTAL_REQUESTS)
        self.assertEqual(len(items), reader.MAX_TOTAL_REQUESTS)
        self.assertEqual(meta["window_coverage"], "capped")
        self.assertEqual(meta["stop_reason"], "request_limit")
        self.assertIn("запрос 5", meta["capped_queries"])

    def test_missing_or_bad_publication_date_is_not_invented(self):
        def fake_fetch(_url):
            body = page([
                act("2001202610020002", "Без даты", date="", documentDate="17.10.2026"),
                act("2001202610030003", "Плохая дата", date="2026-99-99",
                    documentDate="17.10.2026"),
            ])
            return json.dumps(body, ensure_ascii=False).encode(), "application/json"

        items, meta = reader.fetch_documents(
            {"title_queries": ["биометрических"]}, 7,
            dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc), fake_fetch)
        self.assertEqual([item["published"] for item in items], [None, None])
        self.assertEqual(meta["undated"], 2)

    def test_bad_json_html_and_api_error_raise(self):
        now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)

        for body, ctype in [
            (b"<html>maintenance</html>", "text/html"),
            (b"not json", "application/json"),
            (json.dumps({"error": "unavailable"}).encode(), "application/json"),
        ]:
            with self.subTest(content_type=ctype, body=body[:20]):
                def fake_fetch(_url, body=body, ctype=ctype):
                    return body, ctype
                with self.assertRaises(ValueError):
                    reader.fetch_documents({"title_queries": ["AI"]}, 7, now, fake_fetch)


if __name__ == "__main__":
    unittest.main()
