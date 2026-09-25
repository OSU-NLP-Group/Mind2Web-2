"""CacheFileSys: storage, lookup rules, and writers sharing a task directory."""
from __future__ import annotations

import base64
import io
import json
import logging
import random
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from mind2web2.utils.cache_filesys import CacheFileSys, CacheIndexError, _raw_form, _surface_variants, storage_key
from mind2web2.utils.url_tools import normalize_url_keep_case, normalize_url_simple


def png_bytes(color=(255, 0, 0, 128)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (4, 3), color).save(buffer, format="PNG")
    return buffer.getvalue()


def index_on_disk(task_dir: Path) -> dict:
    return json.loads((task_dir / "index.json").read_text(encoding="utf-8"))


def test_pages_are_readable_from_a_new_instance(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    assert cache.put_web("https://example.com/a/#top", "text A", png_bytes()) == "https://example.com/a"
    cache.put_web("https://example.com/b", "text B", base64.b64encode(png_bytes()).decode())
    cache.put_web("https://example.com/c", "text C", "data:image/png;base64," + base64.b64encode(png_bytes()).decode())
    cache.put_pdf("https://example.com/doc.pdf", b"%PDF-1.4 fake")

    reopened = CacheFileSys(str(tmp_path))  # no save step: every put is already on disk
    assert reopened.get_all_urls() == ["https://example.com/a", "https://example.com/b",
                                       "https://example.com/c", "https://example.com/doc.pdf"]
    assert reopened.summary() == {"total_urls": 4, "web_pages": 3, "pdf_pages": 1, "failed_urls": 0}
    text, screenshot = reopened.get_web("http://www.example.com/a")
    assert text == "text A"
    assert Image.open(io.BytesIO(screenshot)).format == "JPEG"
    assert reopened.get_web("https://example.com/b", get_screenshot=False) == ("text B", None)
    assert reopened.get_pdf("https://example.com/doc.pdf") == b"%PDF-1.4 fake"
    assert reopened.has("https://example.com/doc.pdf") == "pdf"
    assert reopened.has("https://example.com/missing") is None
    with pytest.raises(KeyError):
        reopened.get_pdf("https://example.com/a")


@pytest.mark.parametrize("url", [
    "https://example.com/a%23b",      # stored under ".../a#b"
    "https://example.com/a%2520b",    # stored under ".../a%20b"
    "https://example.com/q?x=a%26b",  # stored under "...?x=a&b"
])
def test_pages_whose_storage_key_changes_when_normalized_again_stay_readable(tmp_path, url):
    """Keys that percent-decoding would change again are read from the files named by the key itself."""
    CacheFileSys(str(tmp_path)).put_web(url, "content", png_bytes())
    reopened = CacheFileSys(str(tmp_path))
    [listed] = reopened.get_all_urls()
    assert storage_key(listed) == storage_key(url)
    assert reopened.get_web(url)[0] == reopened.get_web(listed)[0] == "content"


def is_raw(key: str) -> bool:
    return storage_key(key) != key


def reference_lookup(stored: list[str], url: str):
    """The key CacheFileSys.lookup's documented rules find, applied by scanning every stored key."""
    query_key = storage_key(url)
    if is_raw(query_key):
        if query_key in stored:
            return query_key
        return next((k for k in stored if is_raw(k) and _raw_form(k) == _raw_form(query_key)), None)
    plain = [k for k in stored if not is_raw(k)]
    if url in plain:
        return url

    def by_form(normalize):
        match = normalize(url)
        if match in plain:
            return match
        for key in plain:
            try:
                if normalize(key) == match:
                    return key
            except ValueError:
                pass
        return None

    return (by_form(normalize_url_keep_case)
            or next((variant for variant in _surface_variants(url) if variant in plain), None)
            or by_form(normalize_url_simple))


def found_key(cache: CacheFileSys, url: str):
    """The key of the page ``cache.lookup(url)`` returns the URL of."""
    found = cache.lookup(url)
    return storage_key(found) if found is not None else None


def surface_forms(url: str) -> set[str]:
    forms = {url, url + "/", url.replace("https://", "http://"), url.replace("://", "://www."),
             url.upper(), url + "?utm_source=chatgpt.com", url + "#part"}
    return forms | {url.replace("%23", "#"), url.replace("#", "%23")}


def test_lookup_follows_its_rules_as_pages_are_added_replaced_and_removed(tmp_path):
    urls = [f"https://site{i % 7}.org/Page{i % 5}/{p}" for i in range(40)
            for p in ("", "a%23b", "x y", "q?id=3", "q?id=C", "q?id=C%23", "Wiki_(x)")]
    rng = random.Random(0)
    cache = CacheFileSys(str(tmp_path))
    stored: list[str] = []
    for step in range(300):
        url = rng.choice(urls)
        action = rng.random()
        if action < 0.6:
            key = storage_key(cache.put_web(rng.choice(sorted(surface_forms(url))), "t", png_bytes()))
            if key not in stored:
                stored.append(key)
        elif action < 0.8:
            removed = reference_lookup(stored, url)
            assert (cache.remove(url) is not None) == (removed is not None)
            if removed is not None:
                stored.remove(removed)
        for query in surface_forms(rng.choice(urls)):
            assert found_key(cache, query) == reference_lookup(stored, query), query
    reopened = CacheFileSys(str(tmp_path))
    assert [storage_key(url) for url in reopened.get_all_urls()] == stored
    assert all(found_key(reopened, url) == storage_key(url) for url in reopened.get_all_urls())


def test_a_url_finds_the_page_stored_with_its_own_letter_case_first(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/Docs/Page", "title case", png_bytes())
    cache.put_web("https://example.com/DOCS/page", "mixed case", png_bytes())

    assert cache.lookup("http://www.example.com/DOCS/page/#intro") == "https://example.com/DOCS/page"
    assert cache.get_web("https://example.com/Docs/Page?utm_source=x", get_screenshot=False)[0] == "title case"
    assert cache.lookup("https://example.com/docs/PAGE") == "https://example.com/Docs/Page"  # stored first

    cache.remove("https://example.com/Docs/Page")
    assert cache.lookup("https://example.com/Docs/Page") == "https://example.com/DOCS/page"
    assert CacheFileSys(str(tmp_path)).lookup("https://example.com/docs/PAGE") == "https://example.com/DOCS/page"


def test_a_raw_url_without_its_own_page_finds_no_other_page(tmp_path):
    """``?q=C%23`` normalizes to ``?q=C``; the cached ``?q=C`` page is not the page it names."""
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/search?q=C", "C", png_bytes())
    assert cache.lookup("https://example.com/search?q=C%23") is None
    assert cache.lookup("https://example.com/tags/%23python") is None


def test_ignore_case_false_finds_only_pages_and_failures_in_the_urls_own_letter_case(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/Docs/Page", "title case", png_bytes())
    cache.record_failure("https://example.com/Docs/Other", "HTTP 503")

    assert cache.lookup("http://www.example.com/Docs/Page/#intro", ignore_case=False) == "https://example.com/Docs/Page"
    assert cache.lookup("https://example.com/docs/page", ignore_case=False) is None
    assert cache.lookup("https://example.com/docs/page") == "https://example.com/Docs/Page"
    assert cache.failure("https://www.example.com/Docs/Other/", ignore_case=False)["reason"] == "HTTP 503"
    assert cache.failure("https://example.com/docs/other", ignore_case=False) is None
    assert cache.failure("https://example.com/docs/other")["reason"] == "HTTP 503"

    # A page in another letter case hides the record only when case is ignored.
    cache.put_web("https://example.com/docs/other", "lower case", png_bytes())
    assert cache.failure("https://example.com/Docs/Other", ignore_case=False)["reason"] == "HTTP 503"
    assert cache.failure("https://example.com/Docs/Other") is None


def test_raw_keys_match_their_own_urls_and_capture_no_others(tmp_path):
    """A key that percent-decoding would change again is found only through its own URL."""
    cache = CacheFileSys(str(tmp_path))
    for url, label in [("https://www.example.com/search?q=C%23", "C#"), ("https://example.com/search?q=C", "C"),
                       ("https://example.com/a%2520b", "literal %20"), ("https://example.com/a%20b", "space"),
                       ("https://example.com/x//", "x//"), ("https://example.com/tags/%23python", "#python"),
                       ("https://www.example.com/tags", "tags")]:
        cache.put_web(url, label, png_bytes())

    def page(url):
        return cache.get_web(url, get_screenshot=False)[0] if cache.has(url) else None

    assert page("https://www.example.com/search?q=C%23") == "C#"
    assert page("http://example.com/search?q=C%23&utm_source=chatgpt.com") == "C#"
    assert page("https://example.com/search?q=C") == page("https://www.example.com/search?q=c") == "C"
    assert page("https://example.com/a%2520b") == "literal %20"
    assert page("https://example.com/a%20b") == page("https://example.com/a b") == "space"
    assert page("https://example.com/x//") == "x//"
    assert page("https://example.com/x/") is None
    assert page("https://example.com/tags/%23python") == "#python"
    assert page("https://example.com/tags") == page("https://example.com/tags/") == "tags"

    cache.put_web("https://example.com/a%20b", "space, recaptured", png_bytes())
    assert page("https://example.com/a%2520b") == "literal %20"
    assert page("https://example.com/a b") == "space, recaptured"
    for url in cache.get_all_urls():  # every listed URL addresses its own page
        assert cache.put_web(url, page(url), png_bytes()) == url
    assert len(cache.get_all_urls()) == 7



def test_storing_a_page_of_the_other_type_replaces_its_files(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    key = cache.put_web("https://example.com/report", "html", png_bytes())
    cache.put_pdf(key, b"%PDF-1.4")
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json", ".pdf"]
    cache.put_web(key, "html again", png_bytes())
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".jpg", ".json", ".txt"]
    assert index_on_disk(tmp_path) == {key: "web"}


def test_a_url_returned_by_lookup_passed_to_put_replaces_that_entry(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    stored = cache.put_web("https://example.com/a%23b", "old", png_bytes())  # key ".../a#b"
    assert cache.put_web(cache.lookup("https://example.com/a%23b"), "new", png_bytes()) == stored
    assert cache.get_all_urls() == [stored]
    assert cache.get_web("https://example.com/a%23b")[0] == "new"
    assert index_on_disk(tmp_path) == {"https://example.com/a#b": "web"}


def test_remove_deletes_the_entry_and_its_files(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/a", "a", png_bytes())
    cache.put_pdf("https://example.com/b.pdf", b"%PDF")
    assert cache.remove("http://www.example.com/a/") == "web"
    assert cache.remove("https://example.com/a") is None
    assert index_on_disk(tmp_path) == {"https://example.com/b.pdf": "pdf"}
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json", ".pdf"]


def test_remove_deletes_what_is_on_disk_when_another_process_changed_the_page(tmp_path):
    url = "https://example.com/report"
    CacheFileSys(str(tmp_path)).put_web(url, "html", png_bytes())
    manager, other = CacheFileSys(str(tmp_path)), CacheFileSys(str(tmp_path))  # both see a web page
    CacheFileSys(str(tmp_path)).put_pdf(url, b"%PDF-1.4")  # another process replaces it with a PDF
    assert manager.remove(url) == "pdf"
    assert [p.name for p in tmp_path.iterdir()] == ["index.json"]
    assert index_on_disk(tmp_path) == {}
    assert other.remove(url) is None  # already removed on disk
    assert other.has(url) is None


def test_instances_sharing_a_task_keep_each_others_entries(tmp_path):
    crawler, manager = CacheFileSys(str(tmp_path)), CacheFileSys(str(tmp_path))
    crawler.put_web("https://example.com/1", "1", png_bytes())
    manager.put_web("https://example.com/2", "2", png_bytes())
    crawler.put_pdf("https://example.com/3.pdf", b"%PDF")
    manager.remove("https://example.com/2")
    assert index_on_disk(tmp_path) == {"https://example.com/1": "web", "https://example.com/3.pdf": "pdf"}


def test_an_unreadable_index_is_an_error_not_an_empty_cache(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/a", "a", png_bytes())
    (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CacheIndexError, match="restore it, or delete it"):
        CacheFileSys(str(tmp_path))
    files = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(CacheIndexError):
        cache.put_web("https://example.com/b", "b", png_bytes())
    with pytest.raises(CacheIndexError):
        cache.remove("https://example.com/a")
    assert sorted(p.name for p in tmp_path.iterdir()) == files  # nothing written or deleted
    assert (tmp_path / "index.json").read_text(encoding="utf-8") == "{not json"


def test_concurrent_changes_to_one_page_leave_its_entry_and_files_consistent(tmp_path):
    """Writing a page's files and deleting the files of the type it replaces happen under the lock."""
    cache = CacheFileSys(str(tmp_path))
    url = "https://example.com/report"

    def store(i: int) -> None:
        if i % 2:
            cache.put_pdf(url, b"%PDF")
        else:
            cache.put_web(url, "html", png_bytes())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store, range(400)))
    content_type = index_on_disk(tmp_path)[url]
    expected = [".jpg", ".json", ".txt"] if content_type == "web" else [".json", ".pdf"]
    assert sorted(p.suffix for p in tmp_path.iterdir()) == expected
    assert CacheFileSys(str(tmp_path)).has(url) == content_type


def test_threads_sharing_an_instance_lose_no_entries(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: cache.put_pdf(f"https://example.com/{i}.pdf", b"%PDF"), range(200)))
    assert len(cache.get_all_urls()) == len(index_on_disk(tmp_path)) == 200


def _store_pages(task_dir: str, worker: int, count: int) -> None:
    cache = CacheFileSys(task_dir)
    for i in range(count):
        cache.put_pdf(f"https://example.com/{worker}/{i}.pdf", b"%PDF")


def test_processes_sharing_a_task_lose_no_entries(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_store_pages, [str(tmp_path)] * 4, range(4), [40] * 4))
    assert len(index_on_disk(tmp_path)) == len(CacheFileSys(str(tmp_path)).get_all_urls()) == 160


def test_index_entries_without_their_files_are_ignored(tmp_path, caplog):
    cache = CacheFileSys(str(tmp_path))
    key = cache.put_web("https://example.com/a", "a", png_bytes())
    cache.put_pdf("https://example.com/b.pdf", b"%PDF")
    next(tmp_path.glob("*.jpg")).unlink()
    index = index_on_disk(tmp_path)
    index["https://example.com/c"] = "mhtml"
    (tmp_path / "index.json").write_text(json.dumps(index), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        reopened = CacheFileSys(str(tmp_path))
    assert reopened.get_all_urls() == ["https://example.com/b.pdf"]
    assert f"Ignoring index entry for {key}: its files are missing" in caplog.text
    assert "unknown content type 'mhtml'" in caplog.text


def test_failures_are_recorded_matched_and_cleared_by_storing_the_page(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    assert cache.record_failure("https://example.com/a/", "HTTP 503") == "https://example.com/a"
    cache.record_failure("http://www.example.com/a", "blocked: HTTP 403", blocked=True)

    record = CacheFileSys(str(tmp_path)).failure("https://example.com/a#section")
    assert (record["reason"], record["blocked"], record["attempts"]) == ("blocked: HTTP 403", True, 2)
    assert cache.failure_url("http://www.example.com/a/") == "https://example.com/a"
    assert cache.failure_url("https://example.com/b") is None
    assert cache.summary()["failed_urls"] == 1
    assert cache.has("https://example.com/a") is None

    cache.put_web("https://example.com/a", "captured", png_bytes())
    assert cache.failure("https://example.com/a") is None
    assert json.loads((tmp_path / "failures.json").read_text()) == {}


def test_failure_records_stay_consistent_between_processes(tmp_path):
    crawler, manager = CacheFileSys(str(tmp_path)), CacheFileSys(str(tmp_path))

    # A page stored by one process clears the failure another process recorded after it started.
    crawler.record_failure("https://example.com/a", "timed out after 90s")
    manager.put_web("https://example.com/a", "captured by hand", png_bytes())
    assert json.loads((tmp_path / "failures.json").read_text()) == {}

    # A failure recorded for a page that another process has stored is ignored.
    manager.put_web("https://example.com/b", "stored", png_bytes())
    crawler.record_failure("https://example.com/b", "timed out after 90s")
    reopened = CacheFileSys(str(tmp_path))
    assert reopened.failure("https://example.com/b") is None
    assert reopened.failures() == {}
    assert reopened.summary()["failed_urls"] == 0

    # Removing the page deletes that failure too, instead of letting it reappear.
    assert reopened.remove("http://www.example.com/b/") == "web"
    assert reopened.failure("https://example.com/b") is None
    assert json.loads((tmp_path / "failures.json").read_text()) == {}


def test_removing_a_page_keeps_failures_of_other_urls(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/a", "a", png_bytes())
    cache.record_failure("https://example.com/c", "HTTP 503")
    assert cache.remove("https://example.com/a") == "web"
    assert cache.remove("https://example.com/c") is None
    assert list(cache.failures()) == ["https://example.com/c"]


def test_removing_a_page_keeps_the_failure_of_a_url_differing_in_letter_case(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/docs/Page", "page", png_bytes())
    cache.record_failure("https://example.com/docs/page", "HTTP 503")  # its own record: maybe another page
    cache.record_failure("https://example.com/docs/Page/", "timed out")  # this page's URL: hidden
    assert cache.failures() == {}
    assert cache.remove("https://example.com/docs/Page") == "web"
    assert list(cache.failures()) == ["https://example.com/docs/page"]


def test_failure_records_are_matched_by_the_page_rules(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.record_failure("https://example.com/Docs/Page", "HTTP 503")
    cache.record_failure("https://example.com/DOCS/page", "timed out")
    assert cache.failure("http://www.example.com/DOCS/page/")["reason"] == "timed out"
    assert cache.failure("https://example.com/docs/PAGE")["reason"] == "HTTP 503"  # recorded first
    assert cache.record_failure("https://example.com/Docs/Page#x", "HTTP 502") == "https://example.com/Docs/Page"
    assert cache.failure("https://example.com/Docs/Page")["attempts"] == 2
    assert cache.failure("not a url [") is None


def test_failure_records_being_read_are_never_changed(tmp_path, monkeypatch):
    """Evaluation reads failure records on the event loop while worker threads store pages.

    Every change therefore installs new records instead of changing the ones
    a reader may be iterating, and :meth:`CacheFileSys.failure` reads them once.
    """
    installed = []  # (records as installed, a copy taken then)

    class Cache(CacheFileSys):
        def __setattr__(self, name, value):
            if name == "_failures":
                installed.append((value.records, dict(value.records)))
            super().__setattr__(name, value)

    cache = Cache(str(tmp_path))
    cache.put_web("https://example.com/stored", "stored", png_bytes())
    cache.record_failure("https://example.com/a", "HTTP 503")
    CacheFileSys(str(tmp_path)).record_failure("https://example.com/stored", "timed out")  # hidden by the page
    cache.record_failure("https://example.com/b", "HTTP 503")
    cache.put_web("https://example.com/a", "captured", png_bytes())  # clears a's record
    cache.remove("https://example.com/stored")  # deletes the hidden record
    assert len(installed) >= 5  # at construction, and at each of the four changes to the records
    assert all(records == copy for records, copy in installed)

    # A change after failure() found the record does not make it lose the record.
    monkeypatch.setattr(cache, "_is_stored", lambda url, ignore_case=True: cache.clear_failure(url) and False)
    assert cache.failure("https://example.com/b")["reason"] == "HTTP 503"
    assert CacheFileSys(str(tmp_path)).failures() == {}


# ------------------------------------------------------------------ redirects

START, FINAL = "https://example.com/start", "https://example.org/Final"


def test_a_redirected_page_is_stored_once_and_found_by_its_final_url(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    assert cache.put_web(START, "the page", png_bytes(), final_url=FINAL) == START

    for reader in (cache, CacheFileSys(str(tmp_path))):
        assert reader.get_all_urls() == [START]
        assert reader.redirects() == {FINAL: START}
        for spelling in (FINAL, "http://www.example.org/Final/", FINAL + "?utm_source=chatgpt.com", FINAL + "#top"):
            assert reader.lookup(spelling, ignore_case=False) == START, spelling
        assert reader.lookup("https://example.org/final", ignore_case=False) is None  # another page
        assert reader.lookup("https://example.org/final") == START  # letter case disregarded
        assert reader.get_web(FINAL, get_screenshot=False)[0] == "the page"
    assert len(list(tmp_path.glob("*.txt"))) == 1
    assert json.loads((tmp_path / "redirects.json").read_text()) == {FINAL: START}


@pytest.mark.parametrize("final_url", [None, "", START, "http://www.example.com/start/", START + "#top",
                                       START + "?utm_source=x", "about:blank", "chrome-error://chromewebdata/"])
def test_no_redirect_is_recorded_for_the_same_page_or_a_url_that_is_not_http(tmp_path, final_url):
    cache = CacheFileSys(str(tmp_path))
    cache.put_pdf(START, b"%PDF-1.4", final_url=final_url)
    assert cache.redirects() == {} and not (tmp_path / "redirects.json").exists()


def test_storing_a_page_again_replaces_its_redirect_records(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "first capture", png_bytes(), final_url=FINAL)
    cache.put_web(START, "second capture", png_bytes(), final_url="https://example.org/elsewhere")
    assert cache.redirects() == {"https://example.org/elsewhere": START}
    cache.put_web(START, "no redirect", png_bytes())
    assert cache.redirects() == {} and cache.lookup(FINAL) is None
    # The latest capture that ended at a final URL wins it
    cache.put_web(START, "page", png_bytes(), final_url=FINAL)
    cache.put_web("https://example.com/other", "other", png_bytes(), final_url=FINAL)
    assert CacheFileSys(str(tmp_path)).redirects() == {FINAL: "https://example.com/other"}


def test_a_page_stored_under_a_final_url_takes_its_place(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "redirected", png_bytes(), final_url=FINAL)
    cache.put_web(FINAL, "captured directly", png_bytes())
    assert cache.redirects() == {} and cache.get_web(FINAL, get_screenshot=False)[0] == "captured directly"
    # A capture that ends at a stored page's URL records nothing
    cache.put_web("https://example.com/third", "third", png_bytes(), final_url=FINAL + "/")
    assert cache.redirects() == {} and cache.get_web(FINAL, get_screenshot=False)[0] == "captured directly"
    cache.remove(FINAL)
    assert cache.lookup(FINAL) is None  # no redirect record comes back


def test_removing_a_page_deletes_its_redirect_records(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "page", png_bytes(), final_url=FINAL)
    cache.put_web("https://example.com/kept", "kept", png_bytes(), final_url="https://example.org/kept-final")
    assert cache.remove(START) == "web"
    assert cache.lookup(FINAL) is None
    assert CacheFileSys(str(tmp_path)).redirects() == {"https://example.org/kept-final": "https://example.com/kept"}


def test_a_redirect_in_the_query_letter_case_wins_over_a_page_in_another(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web("https://example.com/News", "the news page", png_bytes())
    cache.put_web("https://example.com/other", "the other page", png_bytes(), final_url="https://example.com/news")
    for ignore_case in (True, False):
        assert cache.lookup("https://example.com/news", ignore_case=ignore_case) == "https://example.com/other"
        assert cache.lookup("https://example.com/News", ignore_case=ignore_case) == "https://example.com/News"
    assert cache.lookup("https://example.com/NEWS") == "https://example.com/News"  # pages come first when case differs


def test_a_final_url_keeps_its_failure_record_hidden_until_its_page_is_removed(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    stale = CacheFileSys(str(tmp_path))  # reads the redirect records before the page is stored
    cache.record_failure(FINAL, "HTTP 503")
    cache.put_web(START, "page", png_bytes(), final_url=FINAL)
    assert cache.failures() == {} and cache.failure(FINAL) is None and cache.lookup(FINAL) == START
    assert FINAL in json.loads((tmp_path / "failures.json").read_text())  # kept on disk
    # A process that has not seen the redirect records a failure for the final URL: also hidden
    stale.record_failure(FINAL, "timed out")
    assert CacheFileSys(str(tmp_path)).failures() == {}
    cache.remove(START)
    assert CacheFileSys(str(tmp_path)).failure(FINAL)["reason"] == "timed out"  # applies again


def test_lookup_without_redirects_finds_only_pages_stored_for_the_url(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "page", png_bytes(), final_url=FINAL)
    assert cache.lookup(FINAL, follow_redirects=False) is None
    assert cache.lookup(START, follow_redirects=False) == START


def test_one_final_url_has_one_record_whatever_its_spelling(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "first", png_bytes(), final_url="http://example.org/login")
    cache.put_web("https://example.com/second", "second", png_bytes(), final_url="https://www.example.org/login/")
    assert cache.redirects() == {"https://www.example.org/login": "https://example.com/second"}
    assert cache.lookup("https://example.org/login") == "https://example.com/second"


def test_a_page_another_process_stored_under_the_final_url_prevents_the_record(tmp_path):
    stale = CacheFileSys(str(tmp_path))
    CacheFileSys(str(tmp_path)).put_web("http://www.example.org/Final/", "captured directly", png_bytes())
    stale.put_web(START, "redirected", png_bytes(), final_url=FINAL)
    assert stale.redirects() == {} and CacheFileSys(str(tmp_path)).lookup(FINAL) == "http://www.example.org/Final"


def test_a_final_url_with_an_encoded_hash_is_matched_as_raw_keys_are(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "page", png_bytes(), final_url="https://example.org/search?q=C%23")
    assert cache.lookup("https://example.org/search?q=C%23") == START
    assert cache.lookup("https://example.org/search?q=C") is None


def test_redirect_records_of_several_instances_are_all_kept(tmp_path):
    first, second = CacheFileSys(str(tmp_path)), CacheFileSys(str(tmp_path))
    first.put_web(START, "a", png_bytes(), final_url=FINAL)
    second.put_web("https://example.com/b", "b", png_bytes(), final_url="https://example.org/b-final")
    assert CacheFileSys(str(tmp_path)).redirects() == {FINAL: START,
                                                        "https://example.org/b-final": "https://example.com/b"}


def test_an_unreadable_redirects_file_stops_the_task_before_anything_changes(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(START, "page", png_bytes())
    (tmp_path / "redirects.json").write_text("{not json")
    with pytest.raises(CacheIndexError, match="recorded redirects"):
        CacheFileSys(str(tmp_path))
    with pytest.raises(CacheIndexError):
        cache.put_web("https://example.com/new", "new", png_bytes())
    with pytest.raises(CacheIndexError):
        cache.remove(START)
    assert index_on_disk(tmp_path) == {START: "web"} and len(list(tmp_path.glob("*.txt"))) == 1


def test_a_final_url_that_is_another_spelling_of_a_stored_page_is_that_page(tmp_path):
    cache = CacheFileSys(str(tmp_path))
    cache.put_web(FINAL, "captured directly", png_bytes())
    cache.put_web(START, "redirected", png_bytes(), final_url="http://www.example.org/Final/?utm_source=x")
    assert cache.redirects() == {} and not (tmp_path / "redirects.json").exists()
    # Stored under another spelling of a recorded final URL, a page deletes the record
    cache.put_web(START, "redirected", png_bytes(), final_url="https://example.org/Other")
    cache.put_web("http://www.example.org/Other/", "captured directly", png_bytes())
    assert cache.redirects() == {} and json.loads((tmp_path / "redirects.json").read_text()) == {}
    # A page that differs from the final URL in letter case only is another page
    cache.put_web(START, "redirected", png_bytes(), final_url="https://example.org/final")
    assert cache.redirects() == {"https://example.org/final": START}
