#!/usr/bin/env python3
"""
Rexlibris - A random book generator for libraries. 

Works with any institution using Ex Libris Primo VE.
"""

import argparse
import json
import random
import re
import string
import threading
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import html
import time

# ── Configuration ───────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".primo-random"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class LibraryConfig:
    """Configuration for a Primo VE library instance."""
    name: str
    base_url: str
    vid: str
    tab: str
    scope: str
    institution: str


KNOWN_LIBRARIES: dict[str, LibraryConfig] = {
    "ucl": LibraryConfig(
        name="UCL Library",
        base_url="https://ucl.primo.exlibrisgroup.com",
        vid="44UCL_INST:UCL_VU2",
        tab="UCLLibraryCatalogue",
        scope="MyInst_and_CI",
        institution="44UCL_INST",
    ),
    "imperial": LibraryConfig(
        name="Imperial College Library",
        base_url="https://library-search.imperial.ac.uk",
        vid="44IMP_INST:ICL_VU1",
        tab="Everything",
        scope="MyInst_and_CI",
        institution="44IMP_INST",
    ),
    "cambridge": LibraryConfig(
        name="Cambridge University Library",
        base_url="https://idiscover.lib.cam.ac.uk",
        vid="44CAM_INST:44CAM_PROD",
        tab="LibraryCatalog",
        scope="All_LIBS",
        institution="44CAM_INST",
    ),
    "kcl": LibraryConfig(
        name="King's College London Library",
        base_url="https://librarysearch.kcl.ac.uk",
        vid="44KCL_INST:44KCL_INST",
        tab="Everything",
        scope="MyInst_and_CI",
        institution="44KCL_INST",
    ),
    "shl": LibraryConfig(
        name="Senate House Library (London)",
        base_url="https://search.libraries.london.ac.uk",
        vid="44SHL_INST:SHL",
        tab="LibraryCatalog",
        scope="SHL_SEARCH",
        institution="44SHL_INST",
    ),
}


@dataclass
class AppConfig:
    """Application configuration with active library and saved libraries."""
    active: str | None = None
    libraries: dict = field(default_factory=dict)

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> "AppConfig":
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                return cls(**data)
            except Exception:
                pass
        return cls()

    def get_library(self, key: str | None = None) -> LibraryConfig | None:
        key = key or self.active
        if not key:
            return None
        if key in self.libraries:
            return LibraryConfig(**self.libraries[key])
        if key in KNOWN_LIBRARIES:
            return KNOWN_LIBRARIES[key]
        return None

    def all_libraries(self) -> dict[str, LibraryConfig]:
        libs = {k: LibraryConfig(**v) for k, v in self.libraries.items()}
        libs.update(KNOWN_LIBRARIES)
        return libs

    def add_library(self, key: str, config: LibraryConfig):
        self.libraries[key] = asdict(config)
        self.active = key
        self.save()

    def remove_library(self, key: str) -> bool:
        if key in self.libraries:
            del self.libraries[key]
            if self.active == key:
                self.active = None
            self.save()
            return True
        return False

    def set_active(self, key: str) -> bool:
        if key in self.libraries or key in KNOWN_LIBRARIES:
            self.active = key
            self.save()
            return True
        return False


# ── Material types ──────────────────────────────────────────────────

TYPES = {
    "book": "books", "ebook": "ebooks", "article": "articles",
    "journal": "journals", "newspaper": "newspapers",
    "dissertation": "dissertations", "video": "videos",
    "audio": "audios", "image": "images", "map": "maps",
    "score": "scores", "database": "databases",
    "conference": "conference_proceedings", "dataset": "datasets",
    "review": "reviews", "text": "text_resources",
}

SEARCH_FIELDS = ["any", "title", "sub", "creator"]

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
}


# ── Random word supply ──────────────────────────────────────────────

class WordSupply:
    """Supplies random words from API, with fallback to prevent crashes."""
    
    _API = "https://random-word-api.herokuapp.com/word?number={n}"
    _BATCH = 50
    _LOW = 20
    
    # Fallback words in case API fails or list is empty
    _FALLBACK = [
        "history", "science", "world", "nature", "human", "society", "culture",
        "language", "music", "philosophy", "politics", "economics", "psychology",
        "biology", "physics", "chemistry", "mathematics", "literature", "poetry",
        "fiction", "theory", "modern", "ancient", "art", "design", "education",
        "technology", "environment", "health", "medicine", "travel", "adventure",
    ]

    def __init__(self):
        self._words: list[str] = []
        self._used: set[str] = set()
        self._lock = threading.Lock()
        self._filling = False

    def _fetch(self, n: int) -> list[str]:
        url = self._API.format(n=n)
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                words = json.loads(resp.read().decode())
            return [w.lower() for w in words if w.isalpha() and 3 <= len(w) <= 12]
        except Exception:
            return []

    def _fill_bg(self):
        try:
            new = self._fetch(self._BATCH)
            if new:
                with self._lock:
                    # Only add words we haven't used yet
                    for w in new:
                        if w not in self._used:
                            self._words.append(w)
        finally:
            self._filling = False

    def _maybe_refill(self):
        if not self._filling and len(self._words) < self._LOW:
            self._filling = True
            threading.Thread(target=self._fill_bg, daemon=True).start()

    def prime(self):
        """Initial fetch - blocking to ensure words are ready."""
        words = self._fetch(self._BATCH)
        with self._lock:
            self._words.extend(words)
        self._maybe_refill()

    def get(self) -> str:
        with self._lock:
            if self._words:
                # Pick random word from available
                idx = random.randrange(len(self._words))
                self._words[idx], self._words[-1] = self._words[-1], self._words[idx]
                word = self._words.pop()
                self._used.add(word)
            else:
                # Fallback: pick from fallback list, avoiding recently used
                available = [w for w in self._FALLBACK if w not in self._used]
                if not available:
                    # Reset used set if we've exhausted everything
                    self._used.clear()
                    available = self._FALLBACK
                word = random.choice(available)
                self._used.add(word)
        
        self._maybe_refill()
        return word

    def size(self) -> int:
        with self._lock:
            return len(self._words)

_word_supply = WordSupply()

_word_supply = WordSupply()


# ── Network ─────────────────────────────────────────────────────────

def _build_search_url(
    config: LibraryConfig,
    query: str,
    field: str = "any",
    material_type: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Build the Primo API search URL."""
    params = {
        "vid": config.vid,
        "tab": config.tab,
        "scope": config.scope,
        "q": f"{field},contains,{query}",
        "lang": "en",
        "offset": offset,
        "limit": limit,
        "sort": "rank",
        "pcAvailability": "false",
        "getMore": "0",
        "conVoc": "true",
        "inst": config.institution,
        "skipDelivery": "Y",
        "disableSplitFacets": "true",
    }
    if material_type and material_type in TYPES:
        params["qInclude"] = f"facet_rtype,exact,{TYPES[material_type]}"

    return f"{config.base_url}/primaws/rest/pub/pnxs?{urllib.parse.urlencode(params)}"

def _do_search(url: str, timeout: int = 10) -> tuple[list[dict], str | None]:
    """Execute search and return (docs, error_message)."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("docs", []), None
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return [], f"Connection error: {e.reason}"
    except json.JSONDecodeError as e:
        return [], f"Invalid JSON response: {e}"
    except Exception as e:
        return [], f"Error: {e}"


def _fetch_batch(
    config: LibraryConfig,
    material_type: str | None = None,
    limit: int = 50
) -> list[dict]:
    query = _word_supply.get()
    field = random.choice(SEARCH_FIELDS)
    offset = random.randint(0, 500)
    url = _build_search_url(config, query, field, material_type, offset, limit)
    docs, _ = _do_search(url)
    return docs


def test_config(config: LibraryConfig, verbose: bool = False) -> tuple[bool, str]:
    """
    Test a library configuration with reliable queries.
    Returns (success, message).
    """
    test_queries = ["the", "book", "science", "history", "a"]
    
    for query in test_queries:
        url = _build_search_url(config, query, "any", None, 0, 5)
        
        if verbose:
            print(f"  Testing query '{query}'...")
            print(f"    URL: {url[:100]}...")
        
        docs, error = _do_search(url, timeout=15)
        
        if error:
            if verbose:
                print(f"    Error: {error}")
            continue
        
        if docs:
            return True, f"Success! Found {len(docs)} results for '{query}'"
    
    url = _build_search_url(config, "the", "any", None, 0, 5)
    _, error = _do_search(url)
    
    if error:
        return False, f"API error: {error}"
    else:
        return False, "No results for any test query (config may be incorrect)"


# ── Auto-detect from URL ────────────────────────────────────────────

def detect_from_url(search_url: str) -> tuple[LibraryConfig | None, str | None]:
    """
    Attempt to extract Primo VE config from a search URL.
    Returns (config, error_message).
    """
    try:
        parsed = urllib.parse.urlparse(search_url)
        params = urllib.parse.parse_qs(parsed.query)
        
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        vid = params.get("vid", [None])[0]
        tab = params.get("tab", [None])[0]
        scope = params.get("search_scope", params.get("scope", [None]))[0]
        
        missing = []
        if not vid:
            missing.append("vid")
        if not tab:
            missing.append("tab")
        if not scope:
            missing.append("search_scope")
        
        if missing:
            return None, f"Missing parameters: {', '.join(missing)}"
        
        institution = vid.split(":")[0] if ":" in vid else vid
        
        domain_parts = parsed.netloc.replace(".primo.exlibrisgroup.com", "").split(".")
        name = domain_parts[0].upper() + " Library"
        
        return LibraryConfig(
            name=name,
            base_url=base_url,
            vid=vid,
            tab=tab,
            scope=scope,
            institution=institution,
        ), None
    except Exception as e:
        return None, f"Failed to parse URL: {e}"


def detect_from_api_url(api_url: str) -> tuple[LibraryConfig | None, str | None]:
    """
    Extract config from a Primo API URL (from network inspector).
    Returns (config, error_message).
    """
    try:
        parsed = urllib.parse.urlparse(api_url)
        params = urllib.parse.parse_qs(parsed.query)
        
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        vid = params.get("vid", [None])[0]
        tab = params.get("tab", [None])[0]
        scope = params.get("scope", [None])[0]
        institution = params.get("inst", [None])[0]
        
        missing = []
        if not vid:
            missing.append("vid")
        if not tab:
            missing.append("tab")
        if not scope:
            missing.append("scope")
        if not institution:
            missing.append("inst")
        
        if missing:
            return None, f"Missing parameters: {', '.join(missing)}"
        
        domain_parts = parsed.netloc.replace(".primo.exlibrisgroup.com", "").split(".")
        name = domain_parts[0].upper() + " Library"
        
        return LibraryConfig(
            name=name,
            base_url=base_url,
            vid=vid,
            tab=tab,
            scope=scope,
            institution=institution,
        ), None
    except Exception as e:
        return None, f"Failed to parse URL: {e}"


# ── Record helpers ──────────────────────────────────────────────────

def _record_id(doc: dict) -> str | None:
    ids = doc.get("pnx", {}).get("control", {}).get("recordid", [])
    return ids[0] if ids else None


def record_url(doc: dict, config: LibraryConfig) -> str | None:
    rid = _record_id(doc)
    if not rid:
        return None
    params = {
        "docid": rid,
        "context": "PC" if rid.startswith("cdi_") else "L",
        "vid": config.vid,
        "lang": "en",
        "search_scope": config.scope,
        "tab": config.tab,
    }
    return f"{config.base_url}/discovery/fulldisplay?{urllib.parse.urlencode(params)}"


def format_record(doc: dict, *, verbose: bool = False) -> list[str]:
    disp = doc.get("pnx", {}).get("display", {})

    title = (disp.get("title", ["Unknown"])[0])[:120]
    creator = (disp.get("creator", disp.get("contributor", ["Unknown"])) or ["Unknown"])[0]
    rtype = disp.get("type", ["?"])[0]
    date = disp.get("creationdate", ["n.d."])[0]

    lines = [
        f"  📖 {title}",
        f"     {creator[:80]}",
        f"     {date} · {rtype}",
    ]
    if verbose:
        for key, label in [("publisher", "Publisher"), ("language", "Language")]:
            val = disp.get(key, [None])[0]
            if val:
                lines.append(f"     {label}: {val[:80]}")
        subjects = disp.get("subject", [])
        if subjects:
            lines.append(f"     Subjects: {'; '.join(subjects[:5])}")
        desc = disp.get("description", [None])[0]
        if desc:
            lines.append(f"     {desc[:200]}{'…' if len(desc) > 200 else ''}")
    return lines


def extract_record_data(doc: dict, config: LibraryConfig) -> dict:
    """Extract record data for web display."""
    disp = doc.get("pnx", {}).get("display", {})
    
    title = (disp.get("title", ["Unknown"])[0])[:200]
    creators = disp.get("creator", disp.get("contributor", [])) or []
    creator = creators[0] if creators else "Unknown"
    rtype = disp.get("type", ["unknown"])[0]
    date = disp.get("creationdate", ["n.d."])[0]
    publisher = disp.get("publisher", [None])[0]
    language = disp.get("language", [None])[0]
    subjects = disp.get("subject", [])[:8]
    description = disp.get("description", [None])[0]
    identifier = disp.get("identifier", [])
    
    isbn = None
    for ident in identifier:
        if ident and ('isbn' in ident.lower() or re.match(r'^\d{10,13}$', ident.replace('-', ''))):
            isbn = ident
            break
    
    return {
        "title": title,
        "creator": creator,
        "creators": creators[:5],
        "type": rtype,
        "date": date,
        "publisher": publisher,
        "language": language,
        "subjects": subjects,
        "description": description[:500] if description else None,
        "isbn": isbn,
        "url": record_url(doc, config),
        "record_id": _record_id(doc),
    }


# ── Result pool (background pre-fetch) ─────────────────────────────

class ResultPool:
    def __init__(
        self,
        config: LibraryConfig,
        target: int = 150,
        low_water: int = 30,
        workers: int = 4
    ):
        self._config = config
        self._items: list[dict] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._target = target
        self._low = low_water
        self._workers = workers
        self._filling = False
        self._type: str | None = None

    @property
    def config(self) -> LibraryConfig:
        return self._config

    @config.setter
    def config(self, value: LibraryConfig):
        if value != self._config:
            self._config = value
            self.clear()

    @property
    def material_type(self) -> str | None:
        return self._type

    @material_type.setter
    def material_type(self, value: str | None):
        if value != self._type:
            self._type = value
            self.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self):
        with self._lock:
            self._items.clear()
            self._seen.clear()

    def _add_docs(self, docs: list[dict]):
        with self._lock:
            for d in docs:
                rid = _record_id(d)
                if rid and rid not in self._seen:
                    self._seen.add(rid)
                    self._items.append(d)

    def take(self, n: int = 1) -> list[dict]:
        with self._lock:
            n = min(n, len(self._items))
            picked: list[dict] = []
            for _ in range(n):
                if not self._items:
                    break
                idx = random.randrange(len(self._items))
                self._items[idx], self._items[-1] = self._items[-1], self._items[idx]
                picked.append(self._items.pop())
            return picked

    def _fill(self):
        try:
            batches = max(3, (self._target - self.size()) // 20)
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futs = [
                    pool.submit(_fetch_batch, self._config, self._type)
                    for _ in range(batches)
                ]
                for f in as_completed(futs):
                    docs = f.result()
                    if docs:
                        self._add_docs(docs)
        finally:
            self._filling = False

    def fill_async(self):
        if self._filling or self.size() >= self._target:
            return
        self._filling = True
        threading.Thread(target=self._fill, daemon=True).start()

    def ensure_available(self, n: int = 1):
        if self.size() < n:
            self._filling = False
            self._fill()
        if self.size() < self._low:
            self.fill_async()


# ── Web UI ──────────────────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&family=Titillium+Web:wght@400;600&display=swap');

*, *::before, *::after { box-sizing: border-box; }

:root {
    --serif: 'Crimson Pro', 'Crimson Text', Georgia, 'Times New Roman', serif;
    --sans: 'Titillium Web', 'Helvetica Neue', Helvetica, Arial, sans-serif;
    --mono: 'IBM Plex Mono', 'Consolas', 'Monaco', monospace;
    --bg: #faf9f7;
    --bg-alt: #f3f1ed;
    --text: #2c2c2c;
    --text-light: #666;
    --text-lighter: #888;
    --accent: #8b4513;
    --border: #ddd;
    --border-light: #e8e6e2;
}

html { font-size: 18px; }

body {
    font-family: var(--serif);
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 0;
    line-height: 1.6;
    min-height: 100vh;
}

.container {
    max-width: 720px;
    margin: 0 auto;
    padding: 2rem 1.5rem 4rem;
}

header {
    text-align: center;
    padding: 2rem 0 1.5rem;
    border-bottom: 1px solid var(--border-light);
    margin-bottom: 2rem;
}

h1 {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 2rem;
    letter-spacing: 0.02em;
    margin: 0 0 0.25rem;
    color: var(--text);
}

.subtitle {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-lighter);
    text-transform: uppercase;
    letter-spacing: 0.15em;
}

.library-name {
    font-family: var(--sans);
    font-size: 0.85rem;
    color: var(--text-light);
    margin-top: 0.75rem;
}

.library-name a {
    color: var(--accent);
    text-decoration: none;
    border-bottom: 1px solid transparent;
}

.library-name a:hover {
    border-bottom-color: var(--accent);
}

/* Controls */
.controls {
    display: flex;
    gap: 1rem;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
    margin-bottom: 2rem;
    padding: 1.25rem;
    background: var(--bg-alt);
    border: 1px solid var(--border-light);
}

.btn {
    font-family: var(--sans);
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0.6rem 1.5rem;
    border: 1px solid var(--text);
    background: var(--bg);
    color: var(--text);
    cursor: pointer;
    transition: all 0.15s ease;
    text-decoration: none;
    display: inline-block;
}

.btn:hover {
    background: var(--text);
    color: var(--bg);
}

.btn:active {
    transform: translateY(1px);
}

.btn-primary {
    background: var(--text);
    color: var(--bg);
}

.btn-primary:hover {
    background: #444;
}

.btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

select {
    font-family: var(--mono);
    font-size: 0.75rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    cursor: pointer;
}

select:focus {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
}

/* Results */
.result {
    padding: 1.5rem 0;
    border-bottom: 1px solid var(--border-light);
}

.result:first-child {
    padding-top: 0;
}

.result-title {
    font-family: var(--serif);
    font-size: 1.35rem;
    font-weight: 600;
    line-height: 1.35;
    margin: 0 0 0.5rem;
    color: var(--text);
}

.result-title a {
    color: inherit;
    text-decoration: none;
    border-bottom: 1px solid transparent;
}

.result-title a:hover {
    color: var(--accent);
    border-bottom-color: var(--accent);
}

.result-creator {
    font-family: var(--serif);
    font-style: italic;
    font-size: 1rem;
    color: var(--text);
    margin-bottom: 0.35rem;
}

.result-meta {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-lighter);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.result-meta span {
    margin-right: 1rem;
}

.result-meta span:last-child {
    margin-right: 0;
}

.result-description {
    font-family: var(--serif);
    font-size: 0.95rem;
    color: var(--text-light);
    margin-top: 0.75rem;
    line-height: 1.55;
}

.result-subjects {
    font-family: var(--sans);
    font-size: 0.75rem;
    color: var(--text-lighter);
    margin-top: 0.6rem;
}

.result-subjects span {
    display: inline-block;
    background: var(--bg-alt);
    padding: 0.15rem 0.5rem;
    margin: 0.15rem 0.25rem 0.15rem 0;
    border: 1px solid var(--border-light);
}

.result-link {
    font-family: var(--mono);
    font-size: 0.7rem;
    margin-top: 0.75rem;
}

.result-link a {
    color: var(--accent);
    text-decoration: none;
}

.result-link a:hover {
    text-decoration: underline;
}

/* Status */
.status {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-lighter);
    text-align: center;
    padding: 1rem;
}

.status-bar {
    display: flex;
    justify-content: center;
    gap: 2rem;
    padding: 0.75rem;
    background: var(--bg-alt);
    border: 1px solid var(--border-light);
    margin-top: 2rem;
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--text-lighter);
    text-transform: uppercase;
    letter-spacing: 0.1em;
}

/* Loading */
.loading {
    text-align: center;
    padding: 3rem;
    color: var(--text-light);
}

.loading::after {
    content: '';
    display: inline-block;
    width: 1rem;
    height: 1rem;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-left: 0.75rem;
    vertical-align: middle;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

/* Empty state */
.empty {
    text-align: center;
    # padding: 3rem;
    color: var(--text-lighter);
    font-style: italic;
}

/* Library selection */
.library-select {
    text-align: center;
    padding: 1rem 1rem;
}

.library-select h2 {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 1.4rem;
    margin-bottom: 1.5rem;
}

.library-list {
    list-style: none;
    padding: 0;
    margin: 0 auto;
    max-width: 400px;
}

.library-list li {
    margin: 0.5rem 0;
    position: relative;
}

.library-list a {
    display: block;
    padding: 0.75rem 1rem;
    font-family: var(--sans);
    font-size: 0.9rem;
    color: var(--text);
    text-decoration: none;
    border: 1px solid var(--border);
    background: var(--bg);
    transition: all 0.15s ease;
}

.library-list a:hover {
    background: var(--text);
    color: var(--bg);
    border-color: var(--text);
}

.library-list .lib-key {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-lighter);
    margin-left: 0.5rem;
}

.library-list a:hover .lib-key {
    color: var(--bg-alt);
}

/* Footer */
footer {
    text-align: center;
    padding: 2rem;
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--text-lighter);
    letter-spacing: 0.05em;
}

footer a {
    color: var(--text-light);
    text-decoration: none;
}

footer a:hover {
    text-decoration: underline;
}

/* Add library form */
.add-library {
    max-width: 500px;
    margin: 0 auto;
    padding: 2rem 0;
}

.add-library h2 {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 1.4rem;
    text-align: center;
    margin-bottom: 1.5rem;
}

.add-library .section-label {
    font-family: var(--sans);
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-light);
    margin: 1.5rem 0 0.75rem;
}

.add-library .section-label:first-of-type {
    margin-top: 0;
}

.add-library .hint {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-lighter);
    margin-bottom: 0.75rem;
}

.add-library .divider {
    text-align: center;
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--text-lighter);
    margin: 1.5rem 0;
    position: relative;
}

.add-library .divider::before,
.add-library .divider::after {
    content: '';
    position: absolute;
    top: 50%;
    width: 40%;
    height: 1px;
    background: var(--border-light);
}

.add-library .divider::before { left: 0; }
.add-library .divider::after { right: 0; }

.form-group {
    margin-bottom: 0.75rem;
}

.form-group label {
    display: block;
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-light);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.25rem;
}

.form-group input[type="text"],
.form-group input[type="url"] {
    width: 100%;
    font-family: var(--mono);
    font-size: 0.8rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
}

.form-group input:focus {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
}

.manual-toggle {
    text-align: center;
    margin: 1rem 0;
    font-family: var(--mono);
    font-size: 0.7rem;
}

.manual-toggle a {
    color: var(--text-lighter);
    text-decoration: none;
    border-bottom: 1px dashed var(--text-lighter);
}

.manual-toggle a:hover {
    color: var(--accent);
    border-bottom-color: var(--accent);
}

.form-actions {
    margin-top: 1.5rem;
    display: flex;
    gap: 1rem;
    justify-content: center;
}

.message {
    font-family: var(--sans);
    font-size: 0.85rem;
    padding: 0.75rem 1rem;
    margin-bottom: 1.5rem;
    border: 1px solid;
}

.message-error {
    color: #8b0000;
    background: #fff5f5;
    border-color: #e8c0c0;
}

.message-success {
    color: #2d5f2d;
    background: #f5fff5;
    border-color: #c0e8c0;
}

/* Library list delete */
.library-list .lib-delete {
    position: absolute;
    right: 0.5rem;
    top: 50%;
    transform: translateY(-50%);
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--text-lighter);
    background: var(--bg);
    border: 1px solid var(--border-light);
    padding: 0.15rem 0.5rem;
    cursor: pointer;
    z-index: 1;
}

.library-list .lib-delete:hover {
    color: #8b0000;
    border-color: #8b0000;
    background: #fff5f5;
}

/* Responsive */
@media (max-width: 600px) {
    html { font-size: 16px; }
    .container { padding: 1rem; }
    .controls { flex-direction: column; gap: 0.75rem; }
    .status-bar { flex-direction: column; gap: 0.5rem; }
}
"""

HTML_BASE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — Rexlibris</title>
    <style>{css}</style>
</head>
<body>
    <div class="container">
        {content}
    </div>
    <footer>
        <a href="/">Rexlibris</a> · Made with love by niv.
    </footer>
    {scripts}
</body>
</html>
"""

HTML_HEADER = """
<header>
    <h1>Rexlibris</h1>
    <div class="subtitle">Random discovery for Primo libraries</div>
    {library_info}
</header>
"""

HTML_LIBRARY_SELECT = """
<div class="library-select">
    <h2>Select a Library</h2>
    <ul class="library-list">
        {items}
    </ul>
</div>
"""

HTML_MAIN = """
{header}
<form class="controls" method="GET" action="/random">
    <input type="hidden" name="lib" value="{lib_key}">
    <select name="type" title="Material type">
        <option value="">All types</option>
        {type_options}
    </select>
    <select name="n" title="Number of results">
        <option value="1" {n1_selected}>1 result</option>
        <option value="3" {n3_selected}>3 results</option>
        <option value="5" {n5_selected}>5 results</option>
        <option value="10" {n10_selected}>10 results</option>
    </select>
    <button type="submit" class="btn btn-primary">Discover</button>
</form>
<div id="results">
    {results}
</div>
<div class="status-bar">
    <span>Cache: {pool_size} items</span>
    <span>Words: {word_size}</span>
</div>
"""

HTML_RESULT = """
<div class="result">
    <h2 class="result-title"><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>
    <div class="result-creator">{creator}</div>
    <div class="result-meta">
        <span>{date}</span>
        <span>{type}</span>
        {publisher_html}
        {language_html}
    </div>
    {description_html}
    {subjects_html}
    <div class="result-link"><a href="{url}" target="_blank" rel="noopener">View in catalogue →</a></div>
</div>
"""

HTML_ADD_LIBRARY = """
{header}
<div class="add-library">
    <h2>Add a Library</h2>
    {message}
    <form method="POST" action="/add-library">
        <div class="hint">
            Paste a search URL from your library's Primo page, or an API URL
            from the browser Network tab (look for a request to 'pnxs').
        </div>
        <div class="form-group">
            <label for="url">Primo URL</label>
            <input type="url" id="url" name="url" value="{url_value}" placeholder="https://library.primo.exlibrisgroup.com/discovery/search?...">
        </div>
        <div class="form-group">
            <label for="name">Library name</label>
            <input type="text" id="name" name="name" value="{name_value}" placeholder="My University Library">
        </div>
        <div class="form-group">
            <label for="key">Short key (a-z, 0-9)</label>
            <input type="text" id="key" name="key" value="{key_value}" placeholder="mylib">
        </div>

        <div class="manual-toggle" style="display:{manual_toggle_display}">
            <a href="#" onclick="document.querySelector('.manual-fields').style.display='block';this.parentElement.style.display='none';return false">Enter details manually instead</a>
        </div>

        <div class="manual-fields" style="display:{manual_display}">
            <div class="form-group">
                <label for="base_url">Base URL</label>
                <input type="url" id="base_url" name="base_url" value="{base_url_value}" placeholder="https://library.primo.exlibrisgroup.com">
            </div>
            <div class="form-group">
                <label for="vid">vid</label>
                <input type="text" id="vid" name="vid" value="{vid_value}" placeholder="44EXAMPLE_INST:VU1">
            </div>
            <div class="form-group">
                <label for="tab">tab</label>
                <input type="text" id="tab" name="tab" value="{tab_value}" placeholder="LibraryCatalogue">
            </div>
            <div class="form-group">
                <label for="scope">scope</label>
                <input type="text" id="scope" name="scope" value="{scope_value}" placeholder="MyInst_and_CI">
            </div>
            <div class="form-group">
                <label for="institution">institution</label>
                <input type="text" id="institution" name="institution" value="{institution_value}" placeholder="44EXAMPLE_INST">
            </div>
        </div>

        <div class="form-actions">
            <a href="/?select=1" class="btn">Cancel</a>
            <button type="submit" class="btn btn-primary">Add Library</button>
        </div>
    </form>
</div>
"""


class WebHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the web UI."""
    
    app_config: AppConfig = None
    pools: dict[str, ResultPool] = {}
    pools_lock = threading.Lock()
    
    def log_message(self, format, *args):
        # Quieter logging
        pass
    
    # ── Cookie helpers ──────────────────────────────────────────────

    def _parse_cookies(self) -> dict[str, str]:
        """Parse cookies from the request header."""
        cookie_header = self.headers.get('Cookie', '')
        cookies = {}
        for item in cookie_header.split(';'):
            item = item.strip()
            if '=' in item:
                k, v = item.split('=', 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def _get_user_libraries(self) -> dict[str, dict]:
        """Read user-added libraries from cookie."""
        cookies = self._parse_cookies()
        raw = cookies.get('rexlibris_libs', '')
        if raw:
            try:
                decoded = urllib.parse.unquote(raw)
                return json.loads(decoded)
            except Exception:
                pass
        return {}

    def _get_active_cookie(self) -> str | None:
        """Read the active library key from cookie."""
        cookies = self._parse_cookies()
        val = cookies.get('rexlibris_active', '')
        return urllib.parse.unquote(val) if val else None

    def _make_libs_cookie(self, libs: dict) -> str:
        """Build Set-Cookie header value for user libraries."""
        encoded = urllib.parse.quote(json.dumps(libs, separators=(',', ':')))
        return f'rexlibris_libs={encoded}; Path=/; Max-Age=31536000; SameSite=Lax'

    def _make_active_cookie(self, key: str) -> str:
        """Build Set-Cookie header value for active library."""
        return f'rexlibris_active={urllib.parse.quote(key)}; Path=/; Max-Age=31536000; SameSite=Lax'

    def _clear_active_cookie(self) -> str:
        """Build Set-Cookie header value to clear active library."""
        return 'rexlibris_active=; Path=/; Max-Age=0; SameSite=Lax'

    # ── Library resolution (cookies + defaults) ─────────────────────

    def _get_web_library(self, key: str) -> LibraryConfig | None:
        """Resolve a library by key from defaults or user cookies."""
        if not key:
            return None
        if key in KNOWN_LIBRARIES:
            return KNOWN_LIBRARIES[key]
        user_libs = self._get_user_libraries()
        if key in user_libs:
            return LibraryConfig(**user_libs[key])
        return None

    def _all_web_libraries(self) -> tuple[dict[str, LibraryConfig], set[str]]:
        """
        Return all available libraries and the set of user-added keys.
        Returns (all_libs_dict, user_keys_set).
        """
        libs: dict[str, LibraryConfig] = dict(KNOWN_LIBRARIES)
        user_libs = self._get_user_libraries()
        user_keys: set[str] = set()
        for key, data in user_libs.items():
            if key not in KNOWN_LIBRARIES:
                libs[key] = LibraryConfig(**data)
                user_keys.add(key)
        return libs, user_keys

    # ── Response helpers ────────────────────────────────────────────

    def _send_html(self, content: str, status: int = 200, cookies: list[str] | None = None):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        for c in (cookies or []):
            self.send_header('Set-Cookie', c)
        self.end_headers()
        self.wfile.write(content.encode('utf-8'))
    
    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def _send_redirect(self, location: str):
        self.send_response(303)
        self.send_header('Location', location)
        self.end_headers()
    
    def _get_pool(self, lib_key: str) -> ResultPool | None:
        config = self.app_config.get_library(lib_key)
        if not config:
            return None

        with self.pools_lock:
            if lib_key not in self.pools:
                pool = ResultPool(config)
                pool.material_type = "book"
                pool.fill_async()
                self.pools[lib_key] = pool
            return self.pools[lib_key]
    
    def _render_page(self, title: str, content: str, scripts: str = "") -> str:
        return HTML_BASE.format(
            title=html.escape(title),
            css=CSS,
            content=content,
            scripts=scripts
        )
    
    def _render_library_select(self) -> str:
        # Get all libraries, but distinguishing between built-in and user-saved
        all_libs = self.app_config.all_libraries()
        user_libs = self.app_config.libraries

        items = []
        for key, lib in all_libs.items():
            is_saved = key in user_libs
            source = "(saved)" if is_saved else ""
            delete_btn = ""
            
            # ONLY user-added libraries get the delete button
            if is_saved:
                delete_btn = (
                    f'<form method="POST" action="/remove-library" style="display:inline">'
                    f'<input type="hidden" name="key" value="{html.escape(key)}">'
                    f'<button type="submit" class="lib-delete" title="Remove this library">&times;</button>'
                    f'</form>'
                )
                
            items.append(
                f'<li>'
                f'<a href="/?lib={html.escape(key)}">'
                f'{html.escape(lib.name)}'
                f'<span class="lib-key">{html.escape(key)} {source}</span>'
                f'</a>'
                f'{delete_btn}'
                f'</li>'
            )
        items.append(
            f'<li><a href="/add-library" class="btn" style="text-align:center;margin-top:0.5rem">'
            f'+ Add a library</a></li>'
        )

        content = HTML_HEADER.format(library_info="") + HTML_LIBRARY_SELECT.format(items="\n".join(items))
        return self._render_page("Select Library", content)

    def _render_add_library(self, error: str = "", values: dict = None) -> str:
        values = values or {}
        message = ""
        if error:
            message = f'<div class="message message-error">{html.escape(error)}</div>'

        has_manual = any(values.get(f) for f in ("base_url", "vid", "tab", "scope", "institution"))
        header = HTML_HEADER.format(library_info='<div class="library-name"><a href="/?select=1">&larr; back to libraries</a></div>')
        content = HTML_ADD_LIBRARY.format(
            header=header,
            message=message,
            url_value=html.escape(values.get("url", "")),
            base_url_value=html.escape(values.get("base_url", "")),
            vid_value=html.escape(values.get("vid", "")),
            tab_value=html.escape(values.get("tab", "")),
            scope_value=html.escape(values.get("scope", "")),
            institution_value=html.escape(values.get("institution", "")),
            name_value=html.escape(values.get("name", "")),
            key_value=html.escape(values.get("key", "")),
            manual_display="block" if has_manual else "none",
            manual_toggle_display="none" if has_manual else "block",
        )
        return self._render_page("Add Library", content)

    def _render_main(self, lib_key: str, results: list[dict] = None, material_type: str = None, n: int = 1) -> str:
        config = self.app_config.get_library(lib_key)
        if not config:
            return self._render_library_select()
        
        pool = self._get_pool(lib_key)
        
        # Header with library info
        library_info = f'<div class="library-name">{html.escape(config.name)} · <a href="/?select=1">change</a></div>'
        header = HTML_HEADER.format(library_info=library_info)
        
        # Type options
        type_options = []
        for t in TYPES.keys():
            selected = 'selected' if t == material_type else ''
            type_options.append(f'<option value="{t}" {selected}>{t.title()}</option>')
        
        # Results HTML
        results_html = ""
        if results:
            for r in results:
                publisher_html = f'<span>{html.escape(r["publisher"])}</span>' if r.get("publisher") else ""
                language_html = f'<span>{html.escape(r["language"])}</span>' if r.get("language") else ""
                
                description_html = ""
                if r.get("description"):
                    desc = r["description"]
                    if len(desc) > 300:
                        desc = desc[:300] + "…"
                    description_html = f'<p class="result-description">{html.escape(desc)}</p>'
                
                subjects_html = ""
                if r.get("subjects"):
                    subj_spans = "".join(f'<span>{html.escape(s)}</span>' for s in r["subjects"][:6])
                    subjects_html = f'<div class="result-subjects">{subj_spans}</div>'
                
                results_html += HTML_RESULT.format(
                    title=html.escape(r.get("title", "Unknown")),
                    creator=html.escape(r.get("creator", "Unknown")),
                    date=html.escape(str(r.get("date", "n.d."))),
                    type=html.escape(r.get("type", "unknown")),
                    publisher_html=publisher_html,
                    language_html=language_html,
                    description_html=description_html,
                    subjects_html=subjects_html,
                    url=html.escape(r.get("url", "#")),
                )
        elif results is not None:
            results_html = '<div class="empty">No results found. Try again or change the filter.</div>'
        else:
            results_html = '<div class="empty">Click "Discover" to find something random.</div>'
        
        content = HTML_MAIN.format(
            header=header,
            lib_key=html.escape(lib_key),
            type_options="\n".join(type_options),
            n1_selected='selected' if n == 1 else '',
            n3_selected='selected' if n == 3 else '',
            n5_selected='selected' if n == 5 else '',
            n10_selected='selected' if n == 10 else '',
            results=results_html,
            pool_size=pool.size() if pool else 0,
            word_size=_word_supply.size(),
        )
        
        return self._render_page(config.name, content)
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        
        lib_key = params.get('lib', [self.app_config.active])[0]
        
        if path == '/':
                    if 'select' not in params and lib_key and self.app_config.get_library(lib_key):
                        self._send_html(self._render_main(lib_key))
                    else:
                        self._send_html(self._render_library_select())

        elif path == '/add-library':
            self._send_html(self._render_add_library())

        elif path == '/random':
            if not lib_key:
                self._send_html(self._render_library_select())
                return
            
            config = self.app_config.get_library(lib_key)
            if not config:
                self._send_html(self._render_library_select())
                return
            
            material_type = params.get('type', [None])[0]
            if material_type and material_type not in TYPES:
                material_type = None
            
            n = min(max(int(params.get('n', [5])[0]), 1), 20)
            
            pool = self._get_pool(lib_key)
            if material_type != pool.material_type:
                pool.material_type = material_type
                pool.fill_async()
                time.sleep(0.3)  # Brief wait for initial fetch
            
            pool.ensure_available(n)
            docs = pool.take(n)
            pool.fill_async()
            
            results = [extract_record_data(doc, config) for doc in docs]
            
            self._send_html(self._render_main(lib_key, results, material_type, n))
        
        elif path == '/api/random':
            if not lib_key:
                self._send_json({"error": "No library specified"}, 400)
                return
            
            config = self.app_config.get_library(lib_key)
            if not config:
                self._send_json({"error": "Library not found"}, 404)
                return
            
            material_type = params.get('type', [None])[0]
            n = min(max(int(params.get('n', [1])[0]), 1), 20)
            
            pool = self._get_pool(lib_key)
            if material_type != pool.material_type:
                pool.material_type = material_type
            
            pool.ensure_available(n)
            docs = pool.take(n)
            pool.fill_async()
            
            results = [extract_record_data(doc, config) for doc in docs]
            self._send_json({"results": results, "count": len(results)})
        
        elif path == '/api/status':
            pool = self._get_pool(lib_key) if lib_key else None
            self._send_json({
                "library": lib_key,
                "pool_size": pool.size() if pool else 0,
                "word_supply": _word_supply.size(),
            })
        
        else:
            self.send_error(404, "Not Found")

    def _parse_post_body(self) -> dict[str, str]:
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/add-library':
            fields = self._parse_post_body()
            url = fields.get("url", "").strip()
            config = None
            error = None

            if url:
                # Try search URL first, then API URL
                config, error = detect_from_url(url)
                if not config:
                    config, error2 = detect_from_api_url(url)
                    if not config:
                        error = f"Could not detect config from URL. As search URL: {error} — As API URL: {error2}"
            else:
                # Manual entry
                base_url = fields.get("base_url", "").strip().rstrip("/")
                vid = fields.get("vid", "").strip()
                tab = fields.get("tab", "").strip()
                scope = fields.get("scope", "").strip()
                institution = fields.get("institution", "").strip()

                if not all([base_url, vid, tab, scope, institution]):
                    error = "All fields (base URL, vid, tab, scope, institution) are required for manual entry."
                else:
                    config = LibraryConfig(
                        name="",
                        base_url=base_url,
                        vid=vid,
                        tab=tab,
                        scope=scope,
                        institution=institution,
                    )

            if not config:
                self._send_html(self._render_add_library(error=error, values=fields), 400)
                return

            # Apply custom name or generate one
            custom_name = fields.get("name", "").strip()
            if custom_name:
                config.name = custom_name
            elif not config.name:
                domain_parts = urllib.parse.urlparse(config.base_url).netloc.replace(".primo.exlibrisgroup.com", "").split(".")
                config.name = domain_parts[0].upper() + " Library"

            # Validate key
            key = fields.get("key", "").strip().lower()
            key = re.sub(r'[^a-z0-9_]', '', key)
            if not key:
                key = re.sub(r'[^a-z0-9]', '', config.name.lower())[:12]
            if not key:
                self._send_html(self._render_add_library(error="Could not generate a valid key. Please provide one.", values=fields), 400)
                return
            if key in KNOWN_LIBRARIES:
                self._send_html(self._render_add_library(error=f"'{key}' is a built-in library name. Choose a different key.", values=fields), 400)
                return
            if key in self.app_config.libraries:
                self._send_html(self._render_add_library(error=f"'{key}' already exists. Choose a different key.", values=fields), 400)
                return

            # Test the configuration
            success, message = test_config(config)
            if not success:
                self._send_html(self._render_add_library(error=f"Configuration test failed: {message}", values=fields), 400)
                return

            self.app_config.add_library(key, config)
            self._send_redirect(f"/?lib={urllib.parse.quote(key)}")

        elif path == '/remove-library':
            fields = self._parse_post_body()
            key = fields.get("key", "").strip()
            # Security check: ensure key is not a built-in
            if key and key not in KNOWN_LIBRARIES:
                self.app_config.remove_library(key)
                # Remove cached pool if present
                with self.pools_lock:
                    self.pools.pop(key, None)
            self._send_redirect("/?select=1")

        else:
            self.send_error(404, "Not Found")


def run_web_server(app_config: AppConfig, port: int = 8080):
    """Start the web server."""
    WebHandler.app_config = app_config
    
    # Prime the word supply
    print(f"  ⏳ Loading word supply...")
    _word_supply.prime()
    print(f"  ✓ {_word_supply.size()} random words ready")
    
    # Pre-warm pool for active library if set
    if app_config.active:
        config = app_config.get_library()
        if config:
            print(f"  ⏳ Pre-fetching from {config.name}...")
            pool = ResultPool(config)
            pool.material_type = "book"
            pool.fill_async()
            WebHandler.pools[app_config.active] = pool
    
    server = HTTPServer(('', port), WebHandler)
    print(f"\n  ✓ Server running at http://localhost:{port}")
    print(f"  Press Ctrl+C to stop\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.shutdown()


# ── Interactive setup ───────────────────────────────────────────────

def add_library_interactive(app_config: AppConfig) -> LibraryConfig | None:
    """Interactive wizard to add a new library."""
    print("\n" + "=" * 60)
    print("  📚  Add New Library")
    print("=" * 60)
    
    print("""
  Choose a method:
    [1] Paste a search URL from your library
    [2] Paste an API URL (from browser Network tab)
    [3] Enter details manually
    [q] Cancel
""")
    
    choice = input("  Method: ").strip().lower()
    
    if choice == "q":
        return None
    
    config: LibraryConfig | None = None
    error: str | None = None
    
    if choice == "1":
        print("\n  Instructions:")
        print("    1. Go to your library's Primo search page")
        print("    2. Search for something (e.g., 'hello')")
        print("    3. Copy the full URL from your browser's address bar")
        print()
        url = input("  Paste search URL: ").strip()
        config, error = detect_from_url(url)
        if error:
            print(f"  ✗ {error}")
            print("  Try method 2 or 3 instead.")
            return None
            
    elif choice == "2":
        print("\n  Instructions:")
        print("    1. Go to your library's Primo search page")
        print("    2. Open Developer Tools (F12) → Network tab")
        print("    3. Search for something")
        print("    4. Look for a request containing 'pnxs' in the Name column")
        print("    5. Right-click it → Copy → Copy URL")
        print()
        url = input("  Paste API URL: ").strip()
        config, error = detect_from_api_url(url)
        if error:
            print(f"  ✗ {error}")
            print("  Try method 3 instead.")
            return None
            
    elif choice == "3":
        print("\n  Enter the details below.")
        print("  (Find these in your browser's Network tab → request to 'pnxs')\n")
        
        try:
            name = input("  Library name: ").strip()
            base_url = input("  Base URL (e.g., https://library-search.imperial.ac.uk): ").strip().rstrip("/")
            vid = input("  vid: ").strip()
            tab = input("  tab: ").strip()
            scope = input("  scope: ").strip()
            institution = input("  inst: ").strip()
            
            if not all([name, base_url, vid, tab, scope, institution]):
                print("  ✗ All fields are required")
                return None
                
            config = LibraryConfig(
                name=name,
                base_url=base_url,
                vid=vid,
                tab=tab,
                scope=scope,
                institution=institution,
            )
        except (KeyboardInterrupt, EOFError):
            print("\n  ✗ Cancelled")
            return None
    else:
        print("  ✗ Invalid choice")
        return None
    
    # Show detected config
    print(f"\n  Detected configuration:")
    print(f"    Base URL   : {config.base_url}")
    print(f"    vid        : {config.vid}")
    print(f"    tab        : {config.tab}")
    print(f"    scope      : {config.scope}")
    print(f"    institution: {config.institution}")
    
    # Allow editing the name
    new_name = input(f"\n  Library name [{config.name}]: ").strip()
    if new_name:
        config.name = new_name
    
    # Test the configuration
    print("\n  ⏳ Testing configuration...")
    success, message = test_config(config, verbose=True)
    
    if success:
        print(f"  ✓ {message}")
    else:
        print(f"  ✗ {message}")
        retry = input("  Save anyway? (y/n): ").strip().lower()
        if retry != "y":
            return None
    
    # Save with a key
    default_key = re.sub(r'[^a-z0-9]', '', config.name.lower())[:12]
    key = input(f"  Save as [{default_key}]: ").strip().lower()
    key = re.sub(r'[^a-z0-9_]', '', key or default_key)
    
    if not key:
        print("  ✗ Invalid key")
        return None
    
    if key in KNOWN_LIBRARIES:
        print(f"  ✗ '{key}' is a built-in library name, choose another")
        return None
    
    app_config.add_library(key, config)
    print(f"  ✓ Saved as '{key}'")
    
    return config


def select_library(app_config: AppConfig) -> LibraryConfig | None:
    """Interactive library selection."""
    all_libs = app_config.all_libraries()
    
    if not all_libs:
        print("  No libraries configured. Adding one now...")
        return add_library_interactive(app_config)
    
    print("\n  Available libraries:")
    keys = list(all_libs.keys())
    for i, key in enumerate(keys, 1):
        lib = all_libs[key]
        marker = " *" if key == app_config.active else ""
        source = "(saved)" if key in app_config.libraries else "(built-in)"
        print(f"    [{i}] {key:12} — {lib.name} {source}{marker}")
    print(f"    [a] Add new library")
    print(f"    [q] Quit")
    
    while True:
        choice = input("\n  Select: ").strip().lower()
        
        if choice == "q":
            return None
        
        if choice == "a":
            return add_library_interactive(app_config)
        
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                key = keys[idx]
                app_config.set_active(key)
                return all_libs[key]
        
        if choice in all_libs:
            app_config.set_active(choice)
            return all_libs[choice]
        
        print("  ✗ Invalid choice")


# ── CLI ─────────────────────────────────────────────────────────────

def get_help(lib_name: str) -> str:
    return f"""\
  Commands:
    r  / Enter       Random item → opens in browser
    r N              Pick from N random items (max 20)
    t TYPE           Set material type filter (e.g., t book)
    t                Clear filter
    v                Toggle verbose output
    s                Show status
    lib              Switch/add library
    h                This help
    q                Quit

  Material types:
    {', '.join(TYPES.keys())}

  Current: {lib_name}"""


def main():
    parser = argparse.ArgumentParser(
        description="Random Book Finder for Ex Libris Primo VE Libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                     Interactive mode (setup on first run)
  %(prog)s --web               Start web interface
  %(prog)s --web -p 3000       Web interface on port 3000
  %(prog)s -l ucl              Use UCL library
  %(prog)s --list              Show available libraries
  %(prog)s --add               Add a new library
  %(prog)s --test              Test current library config
  %(prog)s --test -v           Test with verbose output
        """
    )
    parser.add_argument(
        "-l", "--library",
        metavar="KEY",
        help="Use specified library"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available libraries"
    )
    parser.add_argument(
        "--add",
        action="store_true",
        help="Add a new library"
    )
    parser.add_argument(
        "--remove",
        metavar="KEY",
        help="Remove a saved library"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test the current/specified library configuration"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output (for --test)"
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start web interface"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8080,
        help="Port for web interface (default: 8080)"
    )
    args = parser.parse_args()

    app_config = AppConfig.load()

    # Handle --list
    if args.list:
        all_libs = app_config.all_libraries()
        if not all_libs:
            print("No libraries configured. Use --add to add one.")
            return
        print("\nAvailable libraries:")
        for key, lib in all_libs.items():
            marker = " *" if key == app_config.active else ""
            source = "(saved)" if key in app_config.libraries else "(built-in)"
            print(f"  {key:12} — {lib.name} {source}{marker}")
        print(f"\nConfig file: {CONFIG_FILE}")
        return

    # Handle --remove
    if args.remove:
        if args.remove in KNOWN_LIBRARIES:
            print(f"Cannot remove built-in library '{args.remove}'")
            return
        if app_config.remove_library(args.remove):
            print(f"Removed '{args.remove}'")
        else:
            print(f"Library '{args.remove}' not found")
        return

    # Handle --add
    if args.add:
        config = add_library_interactive(app_config)
        if config:
            print(f"\n  Now run: {parser.prog}")
        return

    # Handle --test
    if args.test:
        key = args.library or app_config.active
        if not key:
            print("No library specified. Use -l KEY or set a default first.")
            return
        config = app_config.get_library(key)
        if not config:
            print(f"Library '{key}' not found")
            return
        print(f"Testing {config.name}...")
        print(f"  Base URL   : {config.base_url}")
        print(f"  vid        : {config.vid}")
        print(f"  tab        : {config.tab}")
        print(f"  scope      : {config.scope}")
        print(f"  institution: {config.institution}")
        print()
        success, message = test_config(config, verbose=args.verbose)
        print(f"  {'✓' if success else '✗'} {message}")
        return

    # Handle --web
    if args.web:
        if args.library:
            app_config.set_active(args.library)
        print("\n" + "=" * 60)
        print("  📚  Rexlibris — Web Interface")
        print("=" * 60)
        run_web_server(app_config, args.port)
        return

    # Get library config
    library_config: LibraryConfig | None = None

    if args.library:
        library_config = app_config.get_library(args.library)
        if not library_config:
            print(f"Library '{args.library}' not found")
            print(f"Use --list to see available, or --add to add new")
            return
        app_config.set_active(args.library)
    elif app_config.active:
        library_config = app_config.get_library()
    
    if not library_config:
        library_config = select_library(app_config)
        if not library_config:
            return

    # ── Main interactive loop ───────────────────────────────────────
    
    print("\n" + "=" * 60)
    print(f"  📚  {library_config.name.upper()} — Random Discovery")
    print("=" * 60)

    print("  ⏳ Loading word supply...")
    _word_supply.prime()
    print(f"  ✓ {_word_supply.size()} random words ready")

    pool = ResultPool(library_config)
    verbose = False

    print(get_help(library_config.name))
    print("\n  ⏳ Pre-fetching results...")
    pool.fill_async()

    while True:
        try:
            tag = f":{pool.material_type}" if pool.material_type else ""
            prompt_lib = app_config.active or "lib"
            cmd = input(f"\n[{prompt_lib}{tag}] ({pool.size()})> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye! 📖")
            break

        if not cmd or cmd in ("r", "random"):
            cmd = "r1"

        if cmd[0] == "r" and cmd[1:].isdigit():
            n = max(1, min(int(cmd[1:]), 20))

            if pool.size() < n:
                print("  ⏳ Fetching...")
                pool.ensure_available(n)

            docs = pool.take(n)
            if not docs:
                print("  ✗ No results — try again or change filter.")
                pool.fill_async()
                continue

            pool.fill_async()

            if len(docs) == 1:
                doc = docs[0]
                print()
                print("\n".join(format_record(doc, verbose=verbose)))
                url = record_url(doc, library_config)
                if url:
                    print(f"\n  🔗 {url}")
                    webbrowser.open(url)
            else:
                print()
                for i, doc in enumerate(docs, 1):
                    lines = format_record(doc, verbose=verbose)
                    lines[0] = f"  [{i}]" + lines[0][3:]
                    print("\n".join(lines))
                    print()
                pick = input("  Open # (Enter to skip): ").strip()
                if pick.isdigit() and 1 <= int(pick) <= len(docs):
                    url = record_url(docs[int(pick) - 1], library_config)
                    if url:
                        webbrowser.open(url)
                        print("  🔗 Opened!")

        elif cmd.startswith("t"):
            arg = cmd[1:].strip()
            if not arg or arg in ("none", "all", "clear"):
                pool.material_type = None
                print("  ✓ Filter cleared")
            elif arg in TYPES:
                pool.material_type = arg
                print(f"  ✓ Filter → {arg}")
            else:
                print(f"  ✗ Unknown type: {arg}")
                print("    Types:", ", ".join(TYPES))
                continue
            print("  ⏳ Pre-fetching...")
            pool.fill_async()

        elif cmd in TYPES:
            pool.material_type = cmd
            print(f"  ✓ Filter → {cmd}")
            print("  ⏳ Pre-fetching...")
            pool.fill_async()

        elif cmd in ("v", "verbose"):
            verbose = not verbose
            print(f"  Verbose {'on' if verbose else 'off'}")

        elif cmd in ("s", "status"):
            print(f"  Library : {library_config.name}")
            print(f"  Base URL: {library_config.base_url}")
            print(f"  Filter  : {pool.material_type or '(all)'}")
            print(f"  Cached  : {pool.size()} items")
            print(f"  Words   : {_word_supply.size()} buffered")
            print(f"  Verbose : {'on' if verbose else 'off'}")

        elif cmd in ("lib", "library", "switch"):
            new_config = select_library(app_config)
            if new_config and new_config != library_config:
                library_config = new_config
                pool.config = new_config
                print("  ⏳ Pre-fetching from new library...")
                pool.fill_async()

        elif cmd in ("h", "help", "?"):
            print(get_help(library_config.name))

        elif cmd in ("q", "quit", "exit"):
            print("  Goodbye! 📖")
            break

        else:
            print(f"  Unknown: '{cmd}'  (h for help)")


if __name__ == "__main__":
    main()
