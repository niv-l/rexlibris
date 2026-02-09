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
    _API = "https://random-word-api.vercel.app/api?words={n}&type=uppercase"
    _BATCH = 80
    _LOW = 20

    def __init__(self):
        self._words: list[str] = []
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
                    self._words.extend(new)
        finally:
            self._filling = False

    def _maybe_refill(self):
        if not self._filling and len(self._words) < self._LOW:
            self._filling = True
            threading.Thread(target=self._fill_bg, daemon=True).start()

    def prime(self):
        words = self._fetch(self._BATCH)
        with self._lock:
            self._words.extend(words)
        self._maybe_refill()

    def get(self) -> str:
        with self._lock:
            if self._words:
                idx = random.randrange(len(self._words))
                self._words[idx], self._words[-1] = self._words[-1], self._words[idx]
                word = self._words.pop()
            else:
                word = ""
        self._maybe_refill()
        return word or "".join(random.choices(string.ascii_lowercase, k=random.randint(2, 3)))

    def size(self) -> int:
        with self._lock:
            return len(self._words)


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
    # Test with common words that should exist in any library
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
    
    # All queries failed - try to diagnose
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
        
        # Generate a readable name from the domain
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
