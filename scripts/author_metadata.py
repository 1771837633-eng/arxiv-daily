"""Conservative author/affiliation extraction from public article HTML, without LLM calls."""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parts = []

    def walk(self):
        yield self
        for part in self.parts:
            if isinstance(part, Node):
                yield from part.walk()

    def text(self):
        return " ".join(" ".join(p.text() if isinstance(p, Node) else p for p in self.parts).split())

    def has(self, cls):
        return cls in self.attrs.get("class", "").split()


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].parts.append(node)
        if tag not in {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        self.stack[-1].parts.append(text)


def name_key(name):
    return re.sub(r"[\W\d_]+", "", name).casefold()


def parse_author_html(html, authors, url):
    parser = ArticleHTML()
    parser.feed(html)
    nodes = list(parser.root.walk())
    first = name_key(authors[0]) if authors else ""
    known = {name_key(a): a for a in authors}
    affiliation = ""
    corresponding = []
    contacts = []
    evidence = []
    for creator in (n for n in nodes if n.has("ltx_role_author")):
        names = [n.text() for n in creator.walk() if n.has("ltx_personname")]
        if len(names) != 1 or name_key(names[0]) not in known:
            continue
        key = name_key(names[0])
        author = known[key]
        affs = [n.text() for n in creator.walk() if n.has("ltx_role_affiliation")]
        if key == first and affs and not affiliation:
            affiliation = re.sub(r"^Affiliation\s*:\s*", "", affs[0], flags=re.I)
        notes = [n.text() for n in creator.walk() if n.has("ltx_author_notes") or n.has("ltx_note")]
        explicit = [note for note in notes if re.search(r"\bcorrespond(?:ing\s+author|ence)\b", note, re.I)]
        if explicit:
            corresponding.append(author)
            evidence.extend(explicit)
        if any(n.attrs.get("href", "").startswith("mailto:") for n in creator.walk()):
            contacts.append(author)
    # Highwire metadata associates each institution with the preceding author.
    current_author = ""
    for node in nodes:
        if node.tag != "meta":
            continue
        key = node.attrs.get("name", "").lower()
        value = node.attrs.get("content", "").strip()
        if key == "citation_author":
            current_author = name_key(value)
        elif key == "citation_author_institution" and first and current_author == first and not affiliation:
            affiliation = value
        elif key == "citation_corresponding_author" and name_key(value) in known:
            corresponding.append(known[name_key(value)])
            evidence.append("citation_corresponding_author: " + value)
    return {"first_affiliation": affiliation, "corresponding_authors": [a for a in authors if a in corresponding],
            "contact_authors": list(dict.fromkeys(contacts)), "source_url": url,
            "correspondence_evidence": evidence, "status": "checked",
            "checked_at": datetime.now(timezone.utc).isoformat()}


def enrich_authors(items, previous, config, fetch_html):
    settings = config.get("author_metadata", {})
    if not settings.get("enabled", False):
        return {"requests": 0, "cache_hits": 0, "enabled": False}
    path = Path(settings.get("cache_path", "data/.author_metadata.json"))
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    for paper in previous.get("papers", []):
        meta = paper.get("author_metadata", {})
        if isinstance(meta, dict) and meta.get("fingerprint") and meta.get("status") == "checked":
            cache.setdefault(meta["fingerprint"], meta)
    stats = {"requests": 0, "cache_hits": 0, "deferred": 0, "failures": 0, "enabled": True}
    deadline = time.monotonic() + max(1, int(settings.get("time_budget_seconds", 300)))
    limit = max(0, int(settings.get("max_requests_per_run", 60)))
    try:
        for paper in items:
            authors = paper.get("authors", [])
            fingerprint = hashlib.sha256(json.dumps([1, paper["arxiv_id"], authors], ensure_ascii=False).encode()).hexdigest()
            meta = cache.get(fingerprint, {})
            if not isinstance(meta, dict):
                meta = {}
            try:
                age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(meta.get("checked_at", "")).timestamp()
            except (TypeError, ValueError):
                age = float("inf")
            fresh = isinstance(meta, dict) and age < (7 * 86400 if meta.get("status") == "checked" else 86400)
            if fresh:
                stats["cache_hits"] += 1
            elif stats["requests"] < limit and time.monotonic() < deadline:
                paper_id = paper["arxiv_id"]
                if paper.get("source", "arxiv") == "arxiv" and re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?", paper_id):
                    url = "https://arxiv.org/html/" + paper_id
                elif paper.get("source") == "prb" and re.fullmatch(r"10\.1103/[A-Za-z0-9./_-]+", paper.get("doi", "")):
                    url = "https://journals.aps.org/prb/abstract/" + paper["doi"]
                else:
                    stats["deferred"] += 1
                    continue
                stats["requests"] += 1
                try:
                    html = fetch_html(url, timeout=min(12, max(1, deadline - time.monotonic())), attempts=1)
                    meta = parse_author_html(html.decode("utf-8", errors="replace"), authors, url)
                except (OSError, RuntimeError, ValueError):
                    stats["failures"] += 1
                    meta = {"status": "unavailable", "source_url": url,
                            "checked_at": datetime.now(timezone.utc).isoformat()}
                meta["fingerprint"] = fingerprint
                cache[fingerprint] = meta
            else:
                stats["deferred"] += 1
                if not meta:
                    meta = {"status": "pending"}
            paper["author_metadata"] = meta
            paper["first_affiliation"] = paper.get("first_affiliation") or meta.get("first_affiliation", "")
            names = meta.get("corresponding_authors", [])
            if names:
                paper["corresponding_author"] = "; ".join(names)
    finally:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            print("  [WARN] Could not save author metadata cache; paper results retained")
    return stats
