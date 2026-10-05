"""MeshCore Collector — cracker web-app logic (mode-agnostic, no HTTP).

CrackerApp holds a CollectorStore (and, in live mode, the CollectorCore it
belongs to) and exposes the query + crack + config operations the HTTP layer
calls. It is deliberately framework-free so it can be driven directly from tests.

The crack flow reuses the existing pipeline:
  pick a stored GRP_TXT packet for the target hash -> get its mac_and_data ->
  dictionary/catalog match -> GPU brute-force (CPU fallback) ->
  Channel.from_hashtag(name) -> add to live channels ->
  ChannelCracker.retroactive_decrypt -> persist via store.store_cracked_channel.

Settings (engine preference, charset, max length, catalog toggle, auto-crack,
custom wordlists) are persisted in the store's `settings` table so they survive
restarts. Cracked channels + decoded messages already persist in SQLite.
"""

import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .. import brute_force, brute_force_gpu, multiword
from ..config import DEFAULT_CONFIG_DIR
from ..cracker import BUILTIN_WORDLIST, ChannelCracker, load_catalog
from ..rules import RulesMatcher
from ..crypto import (
    Channel,
    DEFAULT_PUBLIC_CHANNEL_NAME,
    default_public_channel,
    extract_group_payload,
    mac_then_decrypt,
)
from ..store import CollectorStore

# Persisted under this key in store.settings (one JSON blob).
SETTINGS_KEY = "cracker"

# Defaults merged under any persisted settings.
CRACKER_DEFAULTS = {
    "engine": "auto",        # auto | gpu | cpu  (UI preference; "auto" = GPU if present)
    "charset": brute_force.DEFAULT_CHARSET,
    "max_length": 6,
    "use_catalog": True,      # include the bundled ~2.7K-name catalog
    "use_rules": True,        # rule-mangle the wordlist (word+digits/years, hyphenated connectors)
    "auto_crack": False,      # auto-crack pending channels (dictionary, then queue brute-force)
    "auto_dict_only": False,  # restrict auto-crack to dictionary (skip brute-force) for low-power hosts
    "custom_wordlists": [],   # durable paths added via add_wordlist
    # Multiword combiner (GPU-only, explicitly triggered — never in the default/
    # auto sweep, since it's a separate, far larger search).
    "multiword_words": 2,     # N words to combine (1..4)
    "multiword_tier": 10_000, # top-N of the frequency wordlist (the feasibility knob)
    "multiword_concat": True, # try run-together names (#campfire)
    "multiword_hyphen": True, # try hyphenated names (#camp-fire)
}

# Measured throughput of the multiword GPU kernel (~half the charset kernel: per-
# candidate div/mod + variable-length name assembly vs ripple-carry). Used only
# for the UI's wall-clock ETA estimate.
MULTIWORD_HASHRATE = 1.1e9

# A charset x max_length search bigger than this (candidate count) is flagged as
# slow/infeasible in the config UI.
SEARCH_SIZE_WARN = 5_000_000_000

# Re-attempt an exhausted hash once this many new packets have piled up since the
# sweep — a likely sign a different channel now shares the 1-byte hash byte.
EXHAUST_RETRY_PACKET_DELTA = 30

# How many distinct undecoded ciphertext blobs to retain per hash for crack
# targeting (a crack needs one blob plus a few siblings to corroborate a hit).
UNDECODED_BLOB_CAP = 8


class CrackerApp:
    """Cracker operations over a store, optionally attached to a live core.

    Args:
        store: an open CollectorStore (required).
        core: an optional running CollectorCore. When present, cracked
            channels are added to its live decode list and its passive
            ChannelCracker (if any) supplies extra pending hashes.
        use_gpu: hard engine override. ``False`` forces CPU regardless of the
            persisted engine setting (tests and ``--cpu`` use this); ``True``
            prefers GPU when present; ``None`` follows the persisted setting.
    """

    def __init__(self, store: CollectorStore, core=None, use_gpu=None):
        self.store = store
        self.core = core
        self._use_gpu = use_gpu

        # A ChannelCracker bound to the store gives us retroactive_decrypt and
        # the store_cracked_channel persistence. In live mode we prefer the
        # core's own cracker so its known-channel set stays in sync.
        self._cracker = None
        if core is not None and getattr(core, "_cracker", None) is not None:
            self._cracker = core._cracker
        if self._cracker is None:
            self._cracker = ChannelCracker(store)

        # Load persisted settings (merged over defaults).
        self._settings = dict(CRACKER_DEFAULTS)
        try:
            saved = self.store.get_setting(SETTINGS_KEY, None)
            if isinstance(saved, dict):
                self._settings.update(
                    {k: saved[k] for k in CRACKER_DEFAULTS if k in saved}
                )
        except Exception:
            pass

        # Migrate a charset saved before the hyphen was added to the default. Only
        # upgrades the exact old default (lowercase+digits) so a user's custom
        # charset is never touched; closes the gap for hyphenated names on an
        # existing install without them having to re-edit the field.
        if self._settings.get("charset") == "abcdefghijklmnopqrstuvwxyz0123456789":
            self._settings["charset"] = brute_force.DEFAULT_CHARSET
            self._persist_settings()

        # Apply wordlist choices (catalog toggle / custom lists) on startup only
        # when they differ from the already-built default table.
        if not self._settings["use_catalog"] or self._settings["custom_wordlists"]:
            self._apply_wordlist()

        # Rule-based candidates, built lazily on first use (see _ensure_rules).
        self._rules = RulesMatcher(list(load_catalog()) + list(BUILTIN_WORDLIST))

        self._lock = threading.Lock()

        # Crack queue: a single worker thread drains a FIFO of crack jobs, each
        # cancelable. Manual, pasted-packet, and auto-crack requests all enqueue
        # here, so only one grind runs at a time and every job can be canceled.
        self._queue = []          # pending jobs (FIFO)
        self._current = None      # the job the worker is running, or None
        self._history = []        # recently finished jobs (capped)
        self._job_seq = 0
        self._worker = None
        self._worker_stop = threading.Event()
        self._queue_cv = threading.Condition(self._lock)

        # Auto-crack background loop.
        self._auto_thread = None
        self._auto_stop = threading.Event()

        self._status = {
            "running": False,
            "engine": self.engine(),
            "target_hash": None,
            "charset": None,
            "max_length": None,
            "length": 0,
            "total": 0,
            "elapsed": 0.0,
            "result": None,
            "error": None,
            "started_at": None,
        }

        if self._settings["auto_crack"]:
            self._start_auto()

    # --- engine / GPU status -------------------------------------------------

    def gpu_available(self) -> bool:
        """Physical GPU availability, honouring the hard CPU override."""
        if self._use_gpu is False:
            return False
        return brute_force_gpu.is_available()

    def engine(self) -> str:
        """The engine a crack will actually use, given override + preference."""
        if self._use_gpu is False:
            return "cpu"
        pref = self._settings.get("engine", "auto")
        if pref == "cpu":
            return "cpu"
        # "gpu" or "auto": use the GPU when one is present, else fall back.
        return "gpu" if brute_force_gpu.is_available() else "cpu"

    def gpu_status(self) -> dict:
        available = self.gpu_available()
        return {
            "available": available,
            "name": brute_force_gpu.gpu_name() if available else None,
            "engine": self.engine(),
            "forced_cpu": self._use_gpu is False,
        }

    # --- settings / config ---------------------------------------------------

    def get_settings(self) -> dict:
        return dict(self._settings)

    def _persist_settings(self):
        try:
            self.store.set_setting(SETTINGS_KEY, self._settings)
        except Exception:
            pass

    @staticmethod
    def _clean_charset(charset: str) -> str:
        """De-duplicate while preserving order; drop whitespace."""
        seen = []
        for c in charset:
            if c in (" ", "\t", "\n", "\r"):
                continue
            if c not in seen:
                seen.append(c)
        return "".join(seen)

    def update_settings(self, patch: dict) -> dict:
        """Validate and apply a partial settings update; persist; return config()."""
        s = dict(self._settings)
        rebuild = False

        if "engine" in patch and patch["engine"] in ("auto", "gpu", "cpu"):
            s["engine"] = patch["engine"]
        if "charset" in patch and isinstance(patch["charset"], str):
            cleaned = self._clean_charset(patch["charset"])
            if cleaned:
                s["charset"] = cleaned
        if "max_length" in patch:
            try:
                s["max_length"] = max(1, min(int(patch["max_length"]), 12))
            except (TypeError, ValueError):
                pass
        if "use_catalog" in patch:
            new_val = bool(patch["use_catalog"])
            rebuild = rebuild or (new_val != s["use_catalog"])
            s["use_catalog"] = new_val
        if "use_rules" in patch:
            s["use_rules"] = bool(patch["use_rules"])
        if "auto_crack" in patch:
            s["auto_crack"] = bool(patch["auto_crack"])
        if "auto_dict_only" in patch:
            s["auto_dict_only"] = bool(patch["auto_dict_only"])
        if "multiword_words" in patch:
            try:
                s["multiword_words"] = max(1, min(int(patch["multiword_words"]), 4))
            except (TypeError, ValueError):
                pass
        if "multiword_tier" in patch:
            try:
                s["multiword_tier"] = max(1, int(patch["multiword_tier"]))
            except (TypeError, ValueError):
                pass
        if "multiword_concat" in patch:
            s["multiword_concat"] = bool(patch["multiword_concat"])
        if "multiword_hyphen" in patch:
            s["multiword_hyphen"] = bool(patch["multiword_hyphen"])

        self._settings = s
        self._persist_settings()
        if rebuild:
            self._apply_wordlist()

        # Start/stop the auto loop to match the new setting.
        if s["auto_crack"]:
            self._start_auto()
        else:
            self._stop_auto()

        return self.config()

    def estimate_search_size(self, charset: str, max_length: int) -> int:
        """Total brute-force candidate count for lengths 1..max_length."""
        base = len(set(charset)) if charset else 0
        if base == 0:
            return 0
        return sum(base ** length for length in range(1, int(max_length) + 1))

    def wordlist_info(self) -> dict:
        """Counts for the dictionary/catalog panel."""
        return {
            "total": self._cracker.wordlist_size,
            "builtin": len(BUILTIN_WORDLIST),
            "catalog": len(load_catalog()),
            "catalog_enabled": bool(self._settings["use_catalog"]),
            "custom_files": list(self._settings["custom_wordlists"]),
            "rules_enabled": bool(self._settings.get("use_rules")),
            "rules_built": self._rules.built,
            "rules_count": self._rules.count,
        }

    def config(self) -> dict:
        """Everything the config UI needs in one shot."""
        s = self._settings
        size = self.estimate_search_size(s["charset"], s["max_length"])
        return {
            "settings": self.get_settings(),
            "gpu": self.gpu_status(),
            "engine": self.engine(),
            "wordlist": self.wordlist_info(),
            "db_path": str(getattr(self.store, "db_path", "")),
            "search_size": size,
            "search_warn": size > SEARCH_SIZE_WARN,
            "search_warn_threshold": SEARCH_SIZE_WARN,
            "defaults": {
                "charset": brute_force.DEFAULT_CHARSET,
                "max_length": CRACKER_DEFAULTS["max_length"],
            },
            "live": self.core is not None,
            "auto_running": self._auto_thread is not None and self._auto_thread.is_alive(),
        }

    # --- wordlist management -------------------------------------------------

    def _apply_wordlist(self):
        """Rebuild the cracker's candidate table from current settings."""
        try:
            self._cracker.rebuild_wordlist(
                use_catalog=bool(self._settings["use_catalog"]),
                extra_paths=list(self._settings["custom_wordlists"]),
            )
        except Exception:
            pass

    def add_custom_wordlist(self, text=None, path=None) -> dict:
        """Add a custom wordlist, either uploaded text or a path on disk.

        Uploaded text is written to a durable file under the config dir so it
        survives restarts; the path is recorded in settings and re-applied on
        startup. Returns the updated wordlist_info plus the resolved path.
        """
        resolved = None
        if path:
            resolved = str(Path(path))
            if not Path(resolved).is_file():
                return {"ok": False, "error": f"no such file: {resolved}"}
        elif text and text.strip():
            dest_dir = Path(DEFAULT_CONFIG_DIR) / "cracker_wordlists"
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                resolved = str(dest_dir / f"custom-{int(time.time()*1000)}.txt")
                Path(resolved).write_text(text, encoding="utf-8")
            except OSError as e:
                return {"ok": False, "error": f"could not save wordlist: {e}"}
        else:
            return {"ok": False, "error": "provide 'text' or 'path'"}

        lists = list(self._settings["custom_wordlists"])
        if resolved not in lists:
            lists.append(resolved)
        self._settings["custom_wordlists"] = lists
        self._persist_settings()

        before = self._cracker.wordlist_size
        self._cracker.add_wordlist(resolved)
        added = self._cracker.wordlist_size - before

        info = self.wordlist_info()
        info.update({"ok": True, "path": resolved, "added": added})
        return info

    # --- known channels ------------------------------------------------------

    def _known_channels(self):
        """Channels we can already decode: the default public channel, any live
        channels on the core, and previously cracked channels from the DB."""
        channels = [default_public_channel()]
        if self.core is not None:
            channels.extend(getattr(self.core, "_channels", []))
        try:
            for row in self.store.get_cracked_channels():
                try:
                    channels.append(Channel.from_hashtag(row["channel_name"]))
                except Exception:
                    continue
        except Exception:
            pass
        return channels

    # --- pending unknown channels -------------------------------------------

    def pending_channels(self, buckets: Optional[dict] = None) -> list:
        """Hash bytes with still-undecoded GRP_TXT packets — i.e. one or more
        un-cracked channels live on that byte. Each entry carries the byte's
        packet counts and, when a *known* channel also sits on the byte, the
        names it collides with (so the UI can show the relationship instead of
        blaming the known channel for the leftover traffic). Unioned with the
        live cracker's pending set so freshly-seen hashes show up before they
        hit the DB.

        Pass ``buckets`` (a prior :meth:`_scan_buckets` result) to reuse one scan.
        """
        if buckets is None:
            buckets = self._scan_buckets()

        charset = self._settings["charset"]
        max_length = self._settings["max_length"]
        pending = []
        for h, b in buckets.items():
            if b["undecoded"] > 0:
                pending.append({
                    "hash": h,
                    "packet_count": b["total"],
                    "undecoded_count": b["undecoded"],
                    # distinct undecoded messages (relay floods collapsed) — a
                    # truer "how much unknown traffic" than raw packet count.
                    "undecoded_distinct": b["undecoded_distinct"],
                    # known channels sharing this byte (a collision, if any).
                    "collides_with": sorted(b["decoded_by_name"].keys()),
                    # already swept at the current params without a hit?
                    "exhausted": self.is_exhausted(
                        h, charset, max_length, current_packets=b["total"]),
                })

        # Union with the live cracker's pending set (may include hashes whose
        # packets haven't been committed to the store yet).
        seen = {p["hash"] for p in pending}
        if self._cracker is not None:
            for h in self._cracker.pending_hashes:
                if h not in seen:
                    pending.append({"hash": h, "packet_count": 0,
                                    "undecoded_count": 0, "undecoded_distinct": 0,
                                    "collides_with": [], "exhausted": False})
                    seen.add(h)

        pending.sort(key=lambda p: (-p["undecoded_count"], p["hash"]))
        return pending

    def _find_mac_and_data(self, target_hash: int) -> Optional[bytes]:
        """Return mac_and_data from the first stored GRP_TXT packet whose
        channel_hash matches, or None if none is stored."""
        blobs = self._mac_and_data_for_hash(target_hash, limit=1)
        return blobs[0] if blobs else None

    def _packet_count_for_hash(self, target_hash: int) -> int:
        """Count stored GRP_TXT packets with this channel hash."""
        b = self._scan_buckets().get(int(target_hash))
        return b["total"] if b else 0

    def _known_by_hash(self) -> dict:
        """{hash_byte: [Channel]} for every channel we can currently decrypt."""
        idx = defaultdict(list)
        for ch in self._known_channels():
            idx[ch.hash].append(ch)
        return idx

    def _scan_buckets(self) -> dict:
        """Single authoritative scan of stored GRP_TXT packets, grouped by the
        1-byte channel hash. Returns::

            {hash_byte: {
                "total": int,                     # packets on this byte
                "decoded_by_name": {name: count}, # per known channel, precise
                "undecoded": int,                 # no known key verifies these
                "undecoded_distinct": int,        # distinct ciphertext blobs
                "undecoded_blobs": [bytes],       # capped, for crack targeting
            }}

        The hash byte is a *bucket*, not an identity: it is shared by many real
        channels, so leftover packets on a byte that already has a known channel
        belong to a *different*, un-cracked channel — they are the bucket's
        undecoded traffic, never the known channel's. Each decoded packet is
        attributed to the specific key that verifies it, so two known channels
        on one byte are each credited with only their own packets (not a shared
        ``total - undecoded``, which double-counts).

        Counts are decryptability-based, not row-based: the live path stores one
        row per *logical* message (relay copies share it) while retroactive
        decode stores one per packet, so counting rows would miscount. Relay
        duplicates decrypt fine, so they are decoded, not undecoded.
        """
        known = self._known_by_hash()
        buckets = {}
        seen_undecoded = defaultdict(set)  # hash -> set of distinct undecoded blobs
        for pkt in self.store.get_grp_txt_packets():
            raw_hex = pkt.get("raw_hex", "")
            if not raw_hex:
                continue
            try:
                raw = bytes.fromhex(raw_hex)
            except ValueError:
                continue
            extracted = extract_group_payload(raw)
            if not extracted:
                continue
            h = extracted["channel_hash"]
            b = buckets.get(h)
            if b is None:
                b = buckets[h] = {
                    "total": 0, "decoded_by_name": defaultdict(int),
                    "undecoded": 0, "undecoded_distinct": 0, "undecoded_blobs": [],
                }
            b["total"] += 1
            mad = extracted["mac_and_data"]
            decrypter = None
            for ch in known.get(h, ()):
                if mac_then_decrypt(ch.secret, mad) is not None:
                    decrypter = ch
                    break
            if decrypter is not None:
                b["decoded_by_name"][decrypter.name] += 1
            else:
                b["undecoded"] += 1
                blobs = seen_undecoded[h]
                if mad not in blobs:
                    blobs.add(mad)
                    if len(b["undecoded_blobs"]) < UNDECODED_BLOB_CAP:
                        b["undecoded_blobs"].append(mad)
        for h, b in buckets.items():
            b["undecoded_distinct"] = len(seen_undecoded[h])
            b["decoded_by_name"] = dict(b["decoded_by_name"])
        return buckets

    def is_exhausted(self, target_hash, charset, max_length, current_packets=None) -> bool:
        """True if this hash was already swept to >= max_length on this charset
        without a hit, and not enough new packets have arrived to suspect a
        different channel. Subsumption: a prior sweep to length L covers every
        request for length <= L, so re-running a shallower (or equal) depth is a
        no-op."""
        try:
            info = self.store.max_exhausted_length(int(target_hash), charset)
        except Exception:
            return False
        if info is None:
            return False
        swept_len, packets_seen = info
        if swept_len < int(max_length):
            return False  # requested deeper than ever swept -> not covered
        if current_packets is None:
            current_packets = self._packet_count_for_hash(target_hash)
        return current_packets < (packets_seen + EXHAUST_RETRY_PACKET_DELTA)

    def _swept_length(self, target_hash, charset, current_packets=None) -> int:
        """Highest charset length already swept for this hash — the floor for an
        incremental run (grind only lengths above it). Returns 0 when nothing is
        cached, or when the packet count has grown enough since the sweep to
        suspect a *different* channel now shares the byte (re-grind from scratch)."""
        try:
            info = self.store.max_exhausted_length(int(target_hash), charset)
        except Exception:
            return 0
        if info is None:
            return 0
        swept_len, packets_seen = info
        if current_packets is None:
            current_packets = self._packet_count_for_hash(target_hash)
        if current_packets >= packets_seen + EXHAUST_RETRY_PACKET_DELTA:
            return 0
        return swept_len

    def _mac_and_data_for_hash(self, target_hash: int, limit: int = 4,
                               buckets: Optional[dict] = None) -> list:
        """Up to `limit` distinct mac_and_data blobs from *undecoded* stored
        GRP_TXT packets whose channel_hash matches.

        Only undecoded packets: a 1-byte hash can be shared by several real
        channels, so once one is recovered its packets are decoded and must be
        skipped — otherwise a crack of that hash just re-finds the known channel
        and never progresses to the still-encrypted one on the same byte.
        Siblings (blobs[1:]) corroborate a brute-force hit; on a collision hash
        the foreign-channel siblings simply won't decrypt, so they don't help
        or harm (see the verify step).

        Pass ``buckets`` (a prior :meth:`_scan_buckets` result) to reuse one scan
        across many hashes — the sweep/auto loops do this to avoid rescanning per
        hash.
        """
        if buckets is None:
            buckets = self._scan_buckets()
        b = buckets.get(int(target_hash))
        return list(b["undecoded_blobs"])[:limit] if b else []

    # --- cracking ------------------------------------------------------------

    def crack(
        self,
        target_hash: int,
        mac_and_data: Optional[bytes] = None,
        charset: Optional[str] = None,
        max_length: Optional[int] = None,
        cancel_event=None,
        allow_bruteforce: bool = True,
        record_exhausted: bool = True,
    ) -> dict:
        """Synchronously crack the channel name for target_hash.

        Dictionary/catalog match first (instant, reaches long real-world names),
        then GPU brute-force (CPU fallback) unless ``allow_bruteforce`` is False.
        charset/max_length default to the persisted settings when not supplied.
        ``cancel_event`` (a threading.Event) aborts an in-flight brute-force.
        On success the channel is added to the live decode list, all stored
        packets are retroactively decoded, and the result is persisted.

        Returns a result dict (also stored in self._status["result"]).
        """
        if charset is None:
            charset = self._settings["charset"]
        if max_length is None:
            max_length = self._settings["max_length"]
        should_stop = (lambda: cancel_event.is_set()) if cancel_event is not None else None
        engine = self.engine()
        with self._lock:
            self._status.update({
                "running": True,
                "engine": engine,
                "target_hash": target_hash,
                "charset": charset,
                "max_length": max_length,
                "length": 0,
                "total": 0,
                "elapsed": 0.0,
                "result": None,
                "error": None,
                "started_at": time.time(),
            })

        try:
            # Gather sibling packets for the same hash as collision cross-checks.
            siblings = self._mac_and_data_for_hash(target_hash, limit=4)
            if mac_and_data is None:
                mac_and_data = siblings[0] if siblings else None
                extras = siblings[1:]
            else:
                extras = [b for b in siblings if b != mac_and_data][:3]
            if not mac_and_data:
                result = {
                    "cracked": False,
                    "channel_hash": target_hash,
                    "error": "no stored packet for that channel hash",
                }
                return self._finish(result)

            # Instant recovery first — exact dictionary/catalog, then rule
            # mangling — reaching long/structured real-world names (e.g.
            # #wardriving, #weather2024, #bot-tacoma) that charset brute-force
            # at this max_length never would.
            fast_ch, method = self._fast_match(target_hash, mac_and_data)
            if fast_ch is not None:
                decoded_count = self._on_cracked(fast_ch.name, target_hash, method)
                result = {
                    "cracked": True,
                    "channel_name": fast_ch.name,
                    "channel_hash": target_hash,
                    "decoded_count": decoded_count,
                    "method": method,
                }
                return self._finish(result)

            if not allow_bruteforce:
                return self._finish({
                    "cracked": False, "channel_hash": target_hash,
                    "method": "dictionary", "dictionary_only": True,
                })
            if not extras:
                # Only one undecoded packet on this hash: brute-force can't be
                # corroborated by a sibling, so skip it (don't risk a false
                # positive, don't mark exhausted — more packets may arrive).
                return self._finish({
                    "cracked": False, "channel_hash": target_hash,
                    "need_more_packets": True,
                })
            if should_stop is not None and should_stop():
                return self._finish({"cracked": False, "channel_hash": target_hash,
                                     "canceled": True})

            def on_progress(length, total, elapsed):
                with self._lock:
                    self._status["length"] = length
                    self._status["total"] = total
                    self._status["elapsed"] = elapsed

            # Skip lengths already swept for this hash on this charset (a prior
            # max_length=6 run means a 7-char run only grinds length 7).
            min_length = self._swept_length(target_hash, charset) + 1

            if engine == "gpu":
                name = brute_force_gpu.brute_force_channel_gpu(
                    target_hash, mac_and_data, charset=charset,
                    max_length=max_length, on_progress=on_progress,
                    extra_mac_and_data=extras, should_stop=should_stop,
                    min_length=min_length,
                )
            else:
                name = brute_force.brute_force_channel(
                    target_hash, mac_and_data, charset=charset,
                    max_length=max_length, on_progress=on_progress,
                    extra_mac_and_data=extras, should_stop=should_stop,
                    min_length=min_length,
                )

            if name is None:
                canceled = should_stop is not None and should_stop()
                result = {"cracked": False, "channel_hash": target_hash}
                if canceled:
                    result["canceled"] = True
                elif record_exhausted:
                    # Full sweep completed without a hit — remember so auto-crack
                    # doesn't re-grind the same dead end every pass.
                    try:
                        self.store.record_crack_attempt(
                            target_hash, charset, max_length, "exhausted",
                            self._packet_count_for_hash(target_hash),
                        )
                    except Exception:
                        pass
                    result["exhausted"] = True
                return self._finish(result)

            decoded_count = self._on_cracked(name, target_hash, "bruteforce")
            result = {
                "cracked": True,
                "channel_name": name,
                "channel_hash": target_hash,
                "decoded_count": decoded_count,
                "method": "bruteforce",
            }
            return self._finish(result)
        except Exception as e:  # keep the server alive on unexpected failures
            result = {"cracked": False, "channel_hash": target_hash, "error": str(e)}
            return self._finish(result)

    def _ensure_rules(self) -> bool:
        """Build the rules index on first use. Returns True if usable."""
        if not self._settings.get("use_rules"):
            return False
        if not self._rules.built:
            try:
                self._rules.build()
            except Exception:
                return False
        return self._rules.built

    def _fast_match(self, target_hash, mac_and_data):
        """Instant recovery: exact dictionary/catalog, then rule mangling.
        Returns (Channel, method) or (None, None)."""
        ch = self._cracker.match_dictionary(target_hash, mac_and_data)
        if ch is not None:
            return ch, "dictionary"
        if self._ensure_rules():
            ch = self._rules.match(target_hash, mac_and_data)
            if ch is not None:
                return ch, "rules"
        return None, None

    def _on_cracked(self, name: str, target_hash: int, method: str = "?") -> int:
        """Wire a newly cracked channel into the live/decode pipeline and
        persist it (with how it was found). Returns retroactively-decoded count."""
        channel = Channel.from_hashtag(name)

        # Add to the live decode list so future packets decode too.
        if self.core is not None:
            try:
                existing = {ch.name for ch in getattr(self.core, "_channels", [])}
                if channel.name not in existing:
                    self.core.add_channel(channel)
            except Exception:
                pass

        decoded_count = self._cracker.retroactive_decrypt(channel)
        try:
            self.store.store_cracked_channel(channel.name, target_hash,
                                             decoded_count, method)
        except Exception:
            pass
        return decoded_count

    def _finish(self, result: dict) -> dict:
        with self._lock:
            self._status["running"] = False
            self._status["result"] = result
        return result

    # --- crack queue ---------------------------------------------------------

    @staticmethod
    def _job_public(job) -> dict:
        """Serializable view of a job (no internal Event / mac bytes)."""
        return {
            "id": job["id"],
            "kind": job.get("kind", "hash"),
            "target_hash": job["target_hash"],
            "label": job["label"],
            "source": job["source"],
            "status": job["status"],
            "result": job["result"],
            "enqueued_at": job["enqueued_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }

    def enqueue(self, target_hash, mac_and_data=None, charset=None, max_length=None,
                source="manual", allow_bruteforce=True, dedupe=True) -> dict:
        """Add a crack job to the queue; the worker drains it FIFO.

        dedupe skips a hash that is already queued or currently running (used by
        auto-crack so a pending hash isn't enqueued every pass). Pasted-packet
        jobs carry their own mac_and_data and are never deduped.

        Returns {"queued": bool, "job_id": int, "position": int, "reason"?}.
        """
        target_hash = int(target_hash)
        with self._lock:
            if dedupe and mac_and_data is None:
                if self._current and self._current["target_hash"] == target_hash:
                    return {"queued": False, "reason": "already running",
                            "job_id": self._current["id"]}
                for j in self._queue:
                    if j["target_hash"] == target_hash and j["mac_and_data"] is None:
                        return {"queued": False, "reason": "already queued",
                                "job_id": j["id"]}
            self._job_seq += 1
            job = {
                "id": self._job_seq,
                "kind": "hash",
                "target_hash": target_hash,
                "mac_and_data": mac_and_data,
                "charset": charset,
                "max_length": max_length,
                "source": source,
                "allow_bruteforce": allow_bruteforce,
                "label": f"hash 0x{target_hash:02x}",
                "status": "queued",
                "result": None,
                "enqueued_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "cancel": threading.Event(),
            }
            self._queue.append(job)
            position = len(self._queue)
            self._ensure_worker()
            self._queue_cv.notify_all()
        return {"queued": True, "job_id": job["id"], "position": position}

    def enqueue_sweep(self, charset=None, max_length=None, source="manual") -> dict:
        """Enqueue a single batched sweep over all unsolved pending channels.
        Deduped so only one sweep is ever queued/running at a time."""
        with self._lock:
            if self._current is not None and self._current.get("kind") == "sweep":
                return {"queued": False, "reason": "sweep already running",
                        "job_id": self._current["id"]}
            for j in self._queue:
                if j.get("kind") == "sweep":
                    return {"queued": False, "reason": "sweep already queued",
                            "job_id": j["id"]}
            self._job_seq += 1
            job = {
                "id": self._job_seq,
                "kind": "sweep",
                "target_hash": None,
                "mac_and_data": None,
                "charset": charset,
                "max_length": max_length,
                "source": source,
                "allow_bruteforce": True,
                "label": "sweep all pending",
                "status": "queued",
                "result": None,
                "enqueued_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "cancel": threading.Event(),
            }
            self._queue.append(job)
            position = len(self._queue)
            self._ensure_worker()
            self._queue_cv.notify_all()
        return {"queued": True, "job_id": job["id"], "position": position}

    def _mw_params(self, n, tier, include_concat, include_hyphen):
        """Resolve multiword params, falling back to persisted settings."""
        s = self._settings
        return (
            int(n if n is not None else s["multiword_words"]),
            int(tier if tier is not None else s["multiword_tier"]),
            s["multiword_concat"] if include_concat is None else bool(include_concat),
            s["multiword_hyphen"] if include_hyphen is None else bool(include_hyphen),
        )

    def multiword_estimate(self, n=None, tier=None,
                           include_concat=None, include_hyphen=None) -> dict:
        """Exact in-cap candidate count + wall-clock ETA for a multiword run.

        Drives the UI's live estimate so the user sees the (often enormous) size
        before committing — the tier/word-count are the feasibility knob.
        """
        n, tier, include_concat, include_hyphen = self._mw_params(
            n, tier, include_concat, include_hyphen)
        words = multiword.load_words(limit=tier)
        est = multiword.estimate(
            words, n, hashrate=MULTIWORD_HASHRATE,
            include_concat=include_concat, include_hyphen=include_hyphen)
        est.update({
            "n": n, "tier": tier, "tier_available": len(words),
            "include_concat": include_concat, "include_hyphen": include_hyphen,
            "gpu": brute_force_gpu.is_available(),
            "tiers": list(multiword.TIERS),
            "max_words": 4,
        })
        return est

    def enqueue_multiword(self, target_hash=None, n=None, tier=None,
                          include_concat=None, include_hyphen=None,
                          source="manual") -> dict:
        """Enqueue a multiword (word-combination) crack. GPU-only.

        With ``target_hash`` it targets one pending byte; otherwise it sweeps
        every pending byte. Deduped so only one multiword job runs/queues at once.
        """
        n, tier, include_concat, include_hyphen = self._mw_params(
            n, tier, include_concat, include_hyphen)
        with self._lock:
            if self._current is not None and self._current.get("kind") == "multiword":
                return {"queued": False, "reason": "multiword already running",
                        "job_id": self._current["id"]}
            for j in self._queue:
                if j.get("kind") == "multiword":
                    return {"queued": False, "reason": "multiword already queued",
                            "job_id": j["id"]}
            self._job_seq += 1
            th = None if target_hash is None else int(target_hash)
            job = {
                "id": self._job_seq,
                "kind": "multiword",
                "target_hash": th,
                "mac_and_data": None,
                "charset": None,
                "max_length": None,
                "mw_n": n, "mw_tier": tier,
                "mw_concat": include_concat, "mw_hyphen": include_hyphen,
                "source": source,
                "allow_bruteforce": True,
                "label": (f"words 0x{th:02x} ({n}w)" if th is not None
                          else f"word-combo sweep ({n}w)"),
                "status": "queued",
                "result": None,
                "enqueued_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "cancel": threading.Event(),
            }
            self._queue.append(job)
            position = len(self._queue)
            self._ensure_worker()
            self._queue_cv.notify_all()
        return {"queued": True, "job_id": job["id"], "position": position}

    def start_crack(self, target_hash, mac_and_data=None,
                    charset=None, max_length=None) -> dict:
        """Enqueue a manual crack (dictionary-first, then brute-force).

        Kept for the /api/crack endpoint; returns the enqueue result plus a
        legacy ``started`` flag.
        """
        r = self.enqueue(
            target_hash, mac_and_data=mac_and_data,
            charset=charset, max_length=max_length,
            source=("packet" if mac_and_data is not None else "manual"),
            allow_bruteforce=True, dedupe=(mac_and_data is None),
        )
        return {"started": bool(r.get("queued")), "engine": self.engine(), **r}

    def cancel(self, job_id) -> dict:
        """Cancel a queued job (drop it) or the running job (signal it to stop)."""
        job_id = int(job_id)
        with self._lock:
            if self._current is not None and self._current["id"] == job_id:
                self._current["cancel"].set()
                return {"ok": True, "canceled": "running", "job_id": job_id}
            for j in list(self._queue):
                if j["id"] == job_id:
                    j["cancel"].set()
                    j["status"] = "canceled"
                    j["finished_at"] = time.time()
                    self._queue.remove(j)
                    self._history.append(j)
                    self._history = self._history[-50:]
                    return {"ok": True, "canceled": "queued", "job_id": job_id}
        return {"ok": False, "error": "no such job"}

    def cancel_all(self) -> dict:
        """Drop every queued job and signal the running one to stop."""
        with self._lock:
            n = len(self._queue)
            for j in self._queue:
                j["cancel"].set()
                j["status"] = "canceled"
                j["finished_at"] = time.time()
                self._history.append(j)
            self._queue.clear()
            self._history = self._history[-50:]
            canceling = self._current is not None
            if self._current is not None:
                self._current["cancel"].set()
        return {"ok": True, "canceled_queued": n, "canceling_current": canceling}

    def _ensure_worker(self):
        """Start the queue worker if it isn't running. Call under self._lock."""
        if self._worker is None or not self._worker.is_alive():
            self._worker_stop.clear()
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    def _worker_loop(self):
        while not self._worker_stop.is_set():
            with self._queue_cv:
                while not self._queue and not self._worker_stop.is_set():
                    self._queue_cv.wait(timeout=1.0)
                if self._worker_stop.is_set():
                    return
                job = self._queue.pop(0)
                self._current = job
                job["status"] = "running"
                job["started_at"] = time.time()

            if job["cancel"].is_set():
                res = {"cracked": False, "canceled": True,
                       "channel_hash": job["target_hash"]}
            elif job.get("kind") == "sweep":
                res = self._run_sweep(job)
            elif job.get("kind") == "multiword":
                res = self._run_multiword(job)
            else:
                res = self.crack(
                    job["target_hash"], mac_and_data=job["mac_and_data"],
                    charset=job["charset"], max_length=job["max_length"],
                    cancel_event=job["cancel"],
                    allow_bruteforce=job["allow_bruteforce"],
                    # A pasted one-off packet shouldn't mark the hash exhausted.
                    record_exhausted=(job["source"] != "packet"),
                )

            with self._lock:
                job["result"] = res
                job["finished_at"] = time.time()
                if res.get("canceled"):
                    job["status"] = "canceled"
                elif res.get("error"):
                    job["status"] = "failed"
                else:
                    job["status"] = "done"
                self._history.append(job)
                self._history = self._history[-50:]
                self._current = None

    def _run_sweep(self, job) -> dict:
        """Crack every unsolved pending channel in one batched GPU sweep.

        Fast matches (dictionary/rules) are applied immediately; the remainder
        (non-exhausted) go into a single brute_force_batch_gpu pass. Unsolved
        channels are marked exhausted.
        """
        charset = job["charset"] or self._settings["charset"]
        max_length = job["max_length"] or self._settings["max_length"]
        cancel = job["cancel"]
        engine = self.engine()

        with self._lock:
            self._status.update({
                "running": True, "engine": engine, "target_hash": None,
                "charset": charset, "max_length": max_length, "length": 0,
                "total": 0, "elapsed": 0.0, "result": None, "error": None,
                "started_at": time.time(),
            })

        solved_fast = []
        targets = {}
        # One scan feeds the whole loop; cracking one byte never changes another
        # byte's undecoded blobs, so the snapshot stays valid across iterations.
        buckets = self._scan_buckets()
        for p in self.pending_channels(buckets):
            if cancel.is_set():
                break
            h = p["hash"]
            blobs = self._mac_and_data_for_hash(h, limit=4, buckets=buckets)
            if not blobs:
                continue
            fast_ch, method = self._fast_match(h, blobs[0])
            if fast_ch is not None:
                self._on_cracked(fast_ch.name, h, method)
                solved_fast.append(fast_ch.name)
                continue
            if self.is_exhausted(h, charset, max_length,
                                 current_packets=p.get("packet_count")):
                continue
            if len(blobs) < 2:
                continue  # need a sibling to corroborate a brute-force hit
            targets[h] = {"mac_and_data": blobs[0], "extras": blobs[1:]}

        if engine != "gpu" or not targets:
            return self._finish({
                "cracked": bool(solved_fast), "method": "sweep",
                "swept": 0, "found": len(solved_fast),
                "fast": len(solved_fast), "names": list(solved_fast),
                "canceled": cancel.is_set(),
            })

        def on_progress(length, total, elapsed):
            with self._lock:
                self._status["length"] = length
                self._status["total"] = total
                self._status["elapsed"] = elapsed

        # Per-hash incremental floor: a hash already swept to length M only grinds
        # lengths M+1..max_length (a freshly-seen hash still starts at 1).
        min_length_by_hash = {
            h: self._swept_length(h, charset,
                                  current_packets=buckets.get(h, {}).get("total")) + 1
            for h in targets
        }

        try:
            solved = brute_force_gpu.brute_force_batch_gpu(
                targets, charset=charset, max_length=max_length,
                on_progress=on_progress, should_stop=lambda: cancel.is_set(),
                min_length_by_hash=min_length_by_hash,
            )
        except Exception as e:
            return self._finish({"cracked": bool(solved_fast), "method": "sweep",
                                 "error": str(e), "names": list(solved_fast)})

        names = list(solved_fast)
        for hb, name in solved.items():
            self._on_cracked(name, hb, "bruteforce")
            names.append(name)

        if not cancel.is_set():
            for hb in targets:
                if hb not in solved:
                    try:
                        self.store.record_crack_attempt(
                            hb, charset, max_length, "exhausted",
                            self._packet_count_for_hash(hb))
                    except Exception:
                        pass

        return self._finish({
            "cracked": bool(names), "method": "sweep",
            "swept": len(targets), "found": len(solved),
            "fast": len(solved_fast), "names": names,
            "canceled": cancel.is_set(),
        })

    def _multiword_exhausted(self, target_hash, n, tier, include_concat,
                             include_hyphen, current_packets) -> bool:
        """True if an equal-or-larger prior N-word sweep already covered this hash
        (same word count, tier >= this one, separator patterns a superset) and not
        enough new packets have arrived since. Lets a bump from 2->3 words, or
        10K->50K tier, run only the new case and skip what's already done."""
        try:
            cov = self.store.multiword_attempt_covering(
                int(target_hash), int(n), int(tier), include_concat, include_hyphen)
        except Exception:
            return False
        if cov is None:
            return False
        _tier, packets_seen = cov
        return current_packets < (packets_seen + EXHAUST_RETRY_PACKET_DELTA)

    def _run_multiword(self, job) -> dict:
        """Crack pending channels by combining dictionary words (GPU-only).

        Targets one byte (``job['target_hash']``) or sweeps all pending. Keeps its
        own exhaustion cache (``multiword_attempts``), separate from the charset
        one: each word count is its own search, so a miss is recorded per
        (hash, words, tier, concat, hyphen) and a covering prior sweep is skipped.
        """
        cancel = job["cancel"]
        n, tier = job["mw_n"], job["mw_tier"]
        include_concat, include_hyphen = job["mw_concat"], job["mw_hyphen"]

        if self.engine() != "gpu" or not brute_force_gpu.is_available():
            return self._finish({"cracked": False, "method": "multiword",
                                 "error": "multiword cracking requires a GPU"})
        words = multiword.load_words(limit=tier)
        if not words:
            return self._finish({"cracked": False, "method": "multiword",
                                 "error": "wordlist unavailable"})

        with self._lock:
            self._status.update({
                "running": True, "engine": "gpu", "target_hash": job["target_hash"],
                "charset": f"{len(words)} words x{n}", "max_length": n,
                "length": 0, "total": 0, "elapsed": 0.0, "result": None,
                "error": None, "started_at": time.time(),
            })

        buckets = self._scan_buckets()
        only = job["target_hash"]
        targets = {}
        skipped = 0
        for p in self.pending_channels(buckets):
            h = p["hash"]
            if only is not None and h != only:
                continue
            pkts = buckets.get(h, {}).get("total")
            if self._multiword_exhausted(h, n, tier, include_concat,
                                         include_hyphen, pkts):
                skipped += 1
                continue  # already swept at >= these params without a hit
            blobs = self._mac_and_data_for_hash(h, limit=4, buckets=buckets)
            if len(blobs) < 2:
                continue  # need a sibling to corroborate a brute-force hit
            targets[h] = {"mac_and_data": blobs[0], "extras": blobs[1:]}

        if not targets:
            return self._finish({"cracked": False, "method": "multiword",
                                 "swept": 0, "found": 0, "skipped": skipped,
                                 "names": []})

        def on_progress(length, pos, elapsed):
            with self._lock:
                self._status["length"] = length
                self._status["total"] = pos
                self._status["elapsed"] = elapsed

        try:
            solved = brute_force_gpu.brute_force_words_batch_gpu(
                targets, words, n,
                include_concat=include_concat, include_hyphen=include_hyphen,
                on_progress=on_progress, should_stop=lambda: cancel.is_set())
        except Exception as e:
            return self._finish({"cracked": False, "method": "multiword",
                                 "error": str(e)})

        names = []
        for hb, name in solved.items():
            self._on_cracked(name, hb, "multiword")
            names.append(name)

        # Record a miss per target so this exact (or a subsumed) run is skipped
        # next time. Don't record on cancel — the search didn't complete.
        if not cancel.is_set():
            for hb in targets:
                if hb not in solved:
                    try:
                        self.store.record_multiword_attempt(
                            hb, n, tier, include_concat, include_hyphen,
                            packets_seen=buckets.get(hb, {}).get("total", 0))
                    except Exception:
                        pass

        return self._finish({
            "cracked": bool(names), "method": "multiword",
            "swept": len(targets), "found": len(solved), "skipped": skipped,
            "names": names, "canceled": cancel.is_set(),
        })

    def crack_status(self) -> dict:
        """Current crack progress (legacy keys) plus the live queue."""
        with self._lock:
            st = dict(self._status)
            st["active"] = self._job_public(self._current) if self._current else None
            st["queued"] = [self._job_public(j) for j in self._queue]
            st["recent"] = [self._job_public(j) for j in reversed(self._history[-20:])]
            st["queue_len"] = len(self._queue)
        return st

    def health(self) -> dict:
        """Link + device health: live connection state, the durable connection
        event log, and the device boot/crash log. Lets the UI show *why* data
        stopped (device off WiFi, a crash, a reboot) instead of just going stale."""
        connected = bool(getattr(self.core, "is_connected", False)) if self.core else False
        try:
            events = self.store.get_connection_events(limit=50)
        except Exception:
            events = []
        try:
            boots = self.store.get_device_boots(limit=25)
        except Exception:
            boots = []
        last_packet = None
        try:
            last_packet = self.store.get_last_packet_time()
        except Exception:
            pass
        return {
            "live": self.core is not None,
            "connected": connected,
            "last_packet_at": last_packet,
            "connection_events": events,
            "device_boots": boots,
        }

    def _crack_running(self) -> bool:
        with self._lock:
            return self._current is not None

    def exhausted_channels(self) -> list:
        """Hashes already swept without a hit (for the UI's Exhausted state).

        Collapsed to one row per (hash, charset) — the deepest length swept —
        since a sweep to length L subsumes all shallower ones.
        """
        try:
            rows = self.store.get_crack_attempts()
        except Exception:
            return []
        best = {}  # (hash, charset) -> deepest row
        for r in rows:
            key = (r["channel_hash"], r["charset"])
            cur = best.get(key)
            if cur is None or r["max_length"] > cur["max_length"]:
                best[key] = r
        return [{
            "hash": r["channel_hash"],
            "charset": r["charset"],
            "max_length": r["max_length"],
            "result": r["result"],
            "packets_seen": r["packets_seen"],
            "attempted_at": r["attempted_at"],
        } for r in best.values()]

    def retry_hash(self, target_hash) -> dict:
        """Forget exhausted marks for a hash and enqueue a fresh crack."""
        try:
            self.store.clear_crack_attempt(int(target_hash))
        except Exception:
            pass
        r = self.enqueue(int(target_hash), source="manual",
                         allow_bruteforce=True, dedupe=True)
        return {"started": bool(r.get("queued")), **r}

    # --- auto-crack ----------------------------------------------------------

    def auto_crack_once(self) -> list:
        """One auto-crack pass: dictionary-match every pending hash immediately;
        for misses (unless ``auto_dict_only``) enqueue a brute-force job so the
        queue worker grinds them without blocking this loop. Dedup keeps a hash
        from being re-enqueued every pass. Returns the dictionary cracks found
        this pass. Safe to call directly from tests."""
        cracked = []
        # Auto brute-force only when the GPU engine is active: a CPU grind at
        # the default charset/length takes minutes, far too slow to run
        # automatically. CPU hosts stay dictionary/rules-only.
        use_sweep = not bool(self._settings.get("auto_dict_only")) and self.engine() == "gpu"
        charset = self._settings["charset"]
        max_length = self._settings["max_length"]
        remaining = False
        buckets = self._scan_buckets()  # one scan for the whole pass
        for p in self.pending_channels(buckets):
            h = p["hash"]
            blobs = self._mac_and_data_for_hash(h, limit=2, buckets=buckets)  # undecoded packets
            if not blobs:
                continue
            fast_ch, method = self._fast_match(h, blobs[0])
            if fast_ch is not None:
                decoded = self._on_cracked(fast_ch.name, h, method)
                cracked.append({
                    "cracked": True, "channel_name": fast_ch.name,
                    "channel_hash": h, "decoded_count": decoded,
                    "method": method,
                })
                continue
            # Needs a sweep: fast-match missed, has a sibling to corroborate a
            # brute hit, and isn't already swept at these params.
            if use_sweep and len(blobs) >= 2 and not self.is_exhausted(
                    h, charset, max_length, current_packets=p.get("packet_count")):
                remaining = True
        # One batched sweep handles every remaining channel at once.
        if use_sweep and remaining:
            self.enqueue_sweep(source="auto")
        return cracked

    def _auto_loop(self):
        idle_delay = 1.0
        while not self._auto_stop.is_set() and self._settings["auto_crack"]:
            try:
                got = self.auto_crack_once()
            except Exception:
                got = []
            # Tight after progress, back off (capped) while idle.
            if got:
                idle_delay = 1.0
            else:
                idle_delay = min(idle_delay * 1.5, 15.0)
            self._auto_stop.wait(timeout=idle_delay)

    def _start_auto(self):
        if self._auto_thread is not None and self._auto_thread.is_alive():
            return
        self._auto_stop.clear()
        self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self._auto_thread.start()

    def _stop_auto(self):
        self._auto_stop.set()
        t = self._auto_thread
        if t is not None and t.is_alive():
            t.join(timeout=2)
        self._auto_thread = None

    def shutdown(self):
        """Stop background threads (auto-crack loop + queue worker). The store is
        owned by the caller."""
        self._stop_auto()
        self._worker_stop.set()
        with self._lock:
            if self._current is not None:
                self._current["cancel"].set()
            self._queue_cv.notify_all()
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=3)

    # --- results -------------------------------------------------------------

    def channels(self) -> list:
        """Named channels (recovered + the auto-decoded public channel) with
        counts, how they were found, and state. Counts only, cheap at scale."""
        try:
            rows = self.store.get_named_channel_summaries()
        except Exception:
            rows = []
        buckets = self._scan_buckets()
        empty = {"total": 0, "decoded_by_name": {}, "undecoded": 0,
                 "undecoded_distinct": 0}
        out = []
        for row in rows:
            name = row["channel_name"]
            is_public = (name == DEFAULT_PUBLIC_CHANNEL_NAME)
            hb = row.get("channel_hash")
            if hb is None:
                try:
                    hb = (default_public_channel().hash if is_public
                          else Channel.from_hashtag(name).hash)
                except Exception:
                    hb = None
            method = row.get("method") or ("public" if is_public else "?")
            messages = row.get("msg_count") or 0
            b = buckets.get(hb, empty) if hb is not None else empty
            # Packets this channel's *own* key decrypts — not the byte's total
            # minus undecoded, which would credit it with a co-located channel's
            # packets when two known channels share the byte.
            decoded_packets = b["decoded_by_name"].get(name, 0)
            out.append({
                "channel_name": name,
                "channel_hash": hb,
                "state": "public" if is_public else "named",
                "method": method,
                "messages": messages,
                "msg_count": messages,          # legacy alias
                "packets": b["total"],
                "decoded_packets": decoded_packets,
                # Undecoded packets on this byte belong to a *different*,
                # un-cracked channel sharing the 1-byte hash — not to this one.
                # Surfaced as a collision relationship, not as this channel's
                # own failure. The Unknown row for this byte owns the crack.
                "shares_hash": b["undecoded"] > 0,
                "undecoded": b["undecoded"],              # (collision traffic on the byte)
                "undecoded_distinct": b["undecoded_distinct"],
                "unique_senders": row.get("unique_senders") or 0,
                "last_activity": row.get("last_activity"),
                "discovered_at": row.get("discovered_at"),
                "decoded_count": row.get("decoded_count"),
            })
        return out

    def channel_messages(self, channel_name, limit=50, before_id=None,
                         after_id=None, search=None) -> dict:
        """A page of a channel's messages, newest-first, with id cursors + total.

        - ``before_id``: page back to older rows (infinite scroll).
        - ``after_id``: fetch only rows newer than a cursor (incremental live).
        """
        try:
            rows = self.store.page_channel_messages(
                channel_name=channel_name, search=search,
                before_id=before_id, after_id=after_id, limit=limit,
            )
            total = self.store.count_channel_messages(
                channel_name=channel_name, search=search,
            )
        except Exception:
            rows, total = [], 0
        messages = [{
            "id": m.get("id"),
            "sender": m.get("sender"),
            "text": m.get("text"),
            "timestamp": m.get("timestamp"),
            "msg_timestamp": m.get("msg_timestamp"),
        } for m in rows]
        ids = [m["id"] for m in messages if m["id"] is not None]
        return {
            "channel_name": channel_name,
            "total": total,
            "count": len(messages),
            "messages": messages,
            "newest_id": max(ids) if ids else None,
            "oldest_id": min(ids) if ids else None,
            "has_more": len(messages) >= max(1, min(int(limit), 500)),
        }

    def results(self) -> list:
        """Cracked channels with their decoded messages (sender + text).

        Retained for backward compatibility with the original /api/results
        endpoint. Scalable clients should use channels() + channel_messages().
        """
        out = []
        try:
            rows = self.store.get_cracked_channels()
        except Exception:
            rows = []
        for row in rows:
            name = row["channel_name"]
            messages = []
            try:
                for m in self.store.get_channel_messages(channel_name=name, limit=200):
                    messages.append({
                        "sender": m.get("sender"),
                        "text": m.get("text"),
                        "timestamp": m.get("timestamp"),
                        "msg_timestamp": m.get("msg_timestamp"),
                    })
            except Exception:
                pass
            out.append({
                "channel_name": name,
                "channel_hash": row.get("channel_hash"),
                "decoded_count": row.get("decoded_count"),
                "discovered_at": row.get("discovered_at"),
                "messages": messages,
            })
        return out
