"""
MeshCore Collector — GPU brute-force channel cracker (CUDA via CuPy).

Hashtag channels derive their AES-128 key from SHA-256("#name")[:16]. Cracking
a name from an intercepted packet is pure SHA-256 grinding with a cheap reject,
which is exactly what a GPU excels at. This module offloads the grind to CUDA:

  Per candidate (all on-GPU):
    1. psk   = SHA-256("#" + name)[:16]
    2. hash  = SHA-256(psk)[0]            -> reject 255/256 (cleartext filter)
    3. mac   = HMAC-SHA256(psk||0, ct)[:2] -> reject 65535/65536
    4. survivor index written to an output buffer (expected ~1/16M)

The GPU returns only the tiny set of survivors; the host reconstructs each name
and re-verifies with the shared CPU crypto (AES decrypt + plaintext validation)
to rule out the rare 2-byte MAC collision. The public entry point matches
brute_force.brute_force_channel() so callers can treat the two interchangeably.

Requires cupy-cuda12x and an NVIDIA GPU; import-guarded so the module is a no-op
dependency on machines without one (is_available() returns False).
"""

import time
from typing import Callable, Optional

from .crypto import Channel, grp_txt_plaintext_ok, mac_then_decrypt

CIPHER_MAC_SIZE = 2
CIPHER_BLOCK_SIZE = 16

DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"

# Lazily imported so this module loads without cupy present.
_cp = None
_kernel = None
_import_error = None


def _load_cupy():
    global _cp, _import_error
    if _cp is not None:
        return _cp
    if _import_error is not None:
        return None
    try:
        import cupy as cp  # type: ignore
        _cp = cp
        return cp
    except Exception as e:  # ImportError, or CUDA runtime unavailable
        _import_error = e
        return None


def is_available() -> bool:
    """True if CuPy + a CUDA device are usable for GPU cracking."""
    cp = _load_cupy()
    if cp is None:
        return False
    try:
        return cp.cuda.runtime.getDeviceCount() > 0
    except Exception:
        return False


def gpu_name() -> Optional[str]:
    """Human-readable GPU name, or None if unavailable."""
    cp = _load_cupy()
    if cp is None:
        return None
    try:
        props = cp.cuda.runtime.getDeviceProperties(0)
        return props["name"].decode()
    except Exception:
        return None


# CUDA C: single- and multi-block SHA-256, double-hash channel derivation,
# truncated HMAC-SHA256 MAC check. Inputs are tiny, so correctness over micro-
# optimization; the hash-byte reject means the HMAC path runs for only ~1/256
# of threads.
_CUDA_SRC = r"""
extern "C" {

__device__ __constant__ unsigned int KK[64] = {
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

__device__ __forceinline__ unsigned int rotr(unsigned int x, unsigned int n) {
  return (x >> n) | (x << (32 - n));
}

__device__ void sha256_compress(unsigned int h[8], const unsigned char* p) {
  unsigned int w[64];
  #pragma unroll
  for (int i = 0; i < 16; i++) {
    w[i] = (p[i*4] << 24) | (p[i*4+1] << 16) | (p[i*4+2] << 8) | p[i*4+3];
  }
  #pragma unroll
  for (int i = 16; i < 64; i++) {
    unsigned int s0 = rotr(w[i-15],7) ^ rotr(w[i-15],18) ^ (w[i-15] >> 3);
    unsigned int s1 = rotr(w[i-2],17) ^ rotr(w[i-2],19) ^ (w[i-2] >> 10);
    w[i] = w[i-16] + s0 + w[i-7] + s1;
  }
  unsigned int a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
  #pragma unroll
  for (int i = 0; i < 64; i++) {
    unsigned int S1 = rotr(e,6) ^ rotr(e,11) ^ rotr(e,25);
    unsigned int ch = (e & f) ^ ((~e) & g);
    unsigned int t1 = hh + S1 + ch + KK[i] + w[i];
    unsigned int S0 = rotr(a,2) ^ rotr(a,13) ^ rotr(a,22);
    unsigned int maj = (a & b) ^ (a & c) ^ (b & c);
    unsigned int t2 = S0 + maj;
    hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
  }
  h[0]+=a; h[1]+=b; h[2]+=c; h[3]+=d; h[4]+=e; h[5]+=f; h[6]+=g; h[7]+=hh;
}

// SHA-256 over an arbitrary-length message (len up to a few hundred bytes).
__device__ void sha256(const unsigned char* data, unsigned int len, unsigned char out[32]) {
  unsigned int h[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                       0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
  unsigned int full = len / 64;
  for (unsigned int b = 0; b < full; b++) {
    sha256_compress(h, data + b*64);
  }
  unsigned char block[64];
  unsigned int rem = len - full*64;
  for (unsigned int i = 0; i < rem; i++) block[i] = data[full*64 + i];
  block[rem] = 0x80;
  unsigned long long bits = (unsigned long long)len * 8ULL;
  if (rem < 56) {
    for (unsigned int i = rem+1; i < 56; i++) block[i] = 0;
    for (int i = 0; i < 8; i++) block[56+i] = (unsigned char)(bits >> (56 - 8*i));
    sha256_compress(h, block);
  } else {
    for (unsigned int i = rem+1; i < 64; i++) block[i] = 0;
    sha256_compress(h, block);
    unsigned char b2[64];
    for (int i = 0; i < 56; i++) b2[i] = 0;
    for (int i = 0; i < 8; i++) b2[56+i] = (unsigned char)(bits >> (56 - 8*i));
    sha256_compress(h, b2);
  }
  for (int i = 0; i < 8; i++) {
    out[i*4]   = (unsigned char)(h[i] >> 24);
    out[i*4+1] = (unsigned char)(h[i] >> 16);
    out[i*4+2] = (unsigned char)(h[i] >> 8);
    out[i*4+3] = (unsigned char)(h[i]);
  }
}

__global__ void crack(
    const unsigned char* charset, int base, int name_len,
    unsigned long long start, unsigned long long count,
    unsigned char target_hash, unsigned char mac0, unsigned char mac1,
    const unsigned char* ct, int ct_len,
    unsigned long long* out_idx, unsigned int* out_cnt, unsigned int out_max)
{
  unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
  unsigned long long stride = (unsigned long long)gridDim.x * blockDim.x;

  for (unsigned long long i = tid; i < count; i += stride) {
    unsigned long long idx = start + i;

    // index -> name (big-endian digit extraction, matches CPU path)
    unsigned char buf[24];
    buf[0] = '#';
    unsigned long long n = idx;
    for (int pos = name_len - 1; pos >= 0; pos--) {
      buf[1 + pos] = charset[n % base];
      n /= base;
    }

    unsigned char d1[32];
    sha256(buf, name_len + 1, d1);        // psk = d1[:16]
    unsigned char d2[32];
    sha256(d1, 16, d2);                    // channel hash
    if (d2[0] != target_hash) continue;

    // HMAC-SHA256, key = psk(16) || zeros(48), truncated to 2 bytes
    unsigned char ipad[64], opad[64];
    #pragma unroll
    for (int k = 0; k < 64; k++) {
      unsigned char kb = (k < 16) ? d1[k] : 0;
      ipad[k] = kb ^ 0x36;
      opad[k] = kb ^ 0x5c;
    }
    unsigned char inner_msg[64 + 256];
    for (int k = 0; k < 64; k++) inner_msg[k] = ipad[k];
    for (int k = 0; k < ct_len; k++) inner_msg[64 + k] = ct[k];
    unsigned char innerd[32];
    sha256(inner_msg, 64 + ct_len, innerd);

    unsigned char outer_msg[96];
    for (int k = 0; k < 64; k++) outer_msg[k] = opad[k];
    for (int k = 0; k < 32; k++) outer_msg[64 + k] = innerd[k];
    unsigned char macd[32];
    sha256(outer_msg, 96, macd);

    if (macd[0] == mac0 && macd[1] == mac1) {
      unsigned int slot = atomicAdd(out_cnt, 1u);
      if (slot < out_max) out_idx[slot] = idx;
    }
  }
}

// ---- streaming SHA-256 (no large local message buffer) ----
struct Sha { unsigned int h[8]; unsigned long long total; unsigned char buf[64]; int len; };

__device__ void sha_init(Sha* s) {
  s->h[0]=0x6a09e667; s->h[1]=0xbb67ae85; s->h[2]=0x3c6ef372; s->h[3]=0xa54ff53a;
  s->h[4]=0x510e527f; s->h[5]=0x9b05688c; s->h[6]=0x1f83d9ab; s->h[7]=0x5be0cd19;
  s->total = 0; s->len = 0;
}
__device__ void sha_update(Sha* s, const unsigned char* data, int n) {
  s->total += (unsigned long long)n;
  while (n > 0) {
    int take = 64 - s->len; if (take > n) take = n;
    for (int i = 0; i < take; i++) s->buf[s->len + i] = data[i];
    s->len += take; data += take; n -= take;
    if (s->len == 64) { sha256_compress(s->h, s->buf); s->len = 0; }
  }
}
__device__ void sha_final(Sha* s, unsigned char out[32]) {
  unsigned long long bits = s->total * 8ULL;
  s->buf[s->len++] = 0x80;
  if (s->len > 56) { while (s->len < 64) s->buf[s->len++] = 0; sha256_compress(s->h, s->buf); s->len = 0; }
  while (s->len < 56) s->buf[s->len++] = 0;
  for (int i = 0; i < 8; i++) s->buf[56 + i] = (unsigned char)(bits >> (56 - 8*i));
  sha256_compress(s->h, s->buf);
  for (int i = 0; i < 8; i++) {
    out[i*4]   = (unsigned char)(s->h[i] >> 24);
    out[i*4+1] = (unsigned char)(s->h[i] >> 16);
    out[i*4+2] = (unsigned char)(s->h[i] >> 8);
    out[i*4+3] = (unsigned char)(s->h[i]);
  }
}

// ---- batched multi-target sweep ----
// One pass checks each candidate against ALL wanted hash bytes at once. Each
// thread owns a contiguous index range and ripple-carry-increments the name
// (no per-candidate 64-bit div/mod). The double-SHA filter is shared across all
// channels; only candidates whose hash byte is wanted pay the streaming HMAC.
__global__ void crack_batch(
    const unsigned char* charset, int base, int name_len,
    unsigned long long start, unsigned long long count, unsigned long long threads_total,
    const unsigned char* wanted,          // [256] 0/1
    const unsigned char* macs,            // [512] 2 bytes per hash byte
    const int* ct_off, const int* ct_len, const unsigned char* ct_blob,
    unsigned long long* out_idx, unsigned char* out_hash,
    unsigned int* out_cnt, unsigned int out_max)
{
  unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
  if (tid >= threads_total) return;
  unsigned long long per = (count + threads_total - 1) / threads_total;
  unsigned long long lo = start + tid * per;
  unsigned long long end = start + count;
  if (lo >= end) return;
  unsigned long long hi = lo + per; if (hi > end) hi = end;

  unsigned char digits[16];
  unsigned char buf[24];
  buf[0] = '#';
  unsigned long long n = lo;
  for (int pos = name_len - 1; pos >= 0; pos--) {
    int d = (int)(n % base); n /= base;
    digits[pos] = (unsigned char)d; buf[1 + pos] = charset[d];
  }

  for (unsigned long long idx = lo; idx < hi; idx++) {
    unsigned char d1[32];
    sha256(buf, name_len + 1, d1);     // psk = d1[:16]
    unsigned char d2[32];
    sha256(d1, 16, d2);
    unsigned char hb = d2[0];

    if (wanted[hb]) {
      unsigned char ipad[64], opad[64];
      #pragma unroll
      for (int k = 0; k < 64; k++) {
        unsigned char kb = (k < 16) ? d1[k] : 0;
        ipad[k] = kb ^ 0x36; opad[k] = kb ^ 0x5c;
      }
      Sha c;
      unsigned char innerd[32], macd[32];
      int off = ct_off[hb], clen = ct_len[hb];
      sha_init(&c); sha_update(&c, ipad, 64); sha_update(&c, ct_blob + off, clen); sha_final(&c, innerd);
      sha_init(&c); sha_update(&c, opad, 64); sha_update(&c, innerd, 32); sha_final(&c, macd);
      if (macd[0] == macs[2*hb] && macd[1] == macs[2*hb + 1]) {
        unsigned int slot = atomicAdd(out_cnt, 1u);
        if (slot < out_max) { out_idx[slot] = idx; out_hash[slot] = hb; }
      }
    }

    // ripple-carry increment of the name (amortized O(1))
    int pos = name_len - 1;
    while (pos >= 0) {
      int d = digits[pos] + 1;
      if (d < base) { digits[pos] = (unsigned char)d; buf[1 + pos] = charset[d]; break; }
      digits[pos] = 0; buf[1 + pos] = charset[0]; pos--;
    }
  }
}

// ---- multiword sweep ----
// Each candidate is an ordered N-tuple of words from a wordlist blob, joined by a
// separator pattern (a bitmask: bit w set => a hyphen after word w). The linear
// index decodes as: pattern = patterns[idx % Peff]; the remaining digits (base W)
// select the words. The "#"+name is assembled inline and skipped if it exceeds
// max_name_chars (incl. the '#') — the on-device field limit, so an over-length
// name can never match. Same shared double-SHA filter + per-wanted HMAC as crack_batch.
__global__ void crack_words(
    const unsigned char* blob, const int* woff, const int* wlen, int W, int n,
    const unsigned int* patterns, int Peff, int max_name_chars,
    unsigned long long start, unsigned long long count, unsigned long long threads_total,
    const unsigned char* wanted, const unsigned char* macs,
    const int* ct_off, const int* ct_len, const unsigned char* ct_blob,
    unsigned long long* out_idx, unsigned char* out_hash,
    unsigned int* out_cnt, unsigned int out_max)
{
  unsigned long long tid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
  if (tid >= threads_total) return;
  unsigned long long per = (count + threads_total - 1) / threads_total;
  unsigned long long lo = start + tid * per;
  unsigned long long end = start + count;
  if (lo >= end) return;
  unsigned long long hi = lo + per; if (hi > end) hi = end;

  for (unsigned long long idx = lo; idx < hi; idx++) {
    unsigned int pattern = patterns[idx % (unsigned long long)Peff];
    unsigned long long rest = idx / (unsigned long long)Peff;
    int wi[8];
    for (int pos = n - 1; pos >= 0; pos--) {
      wi[pos] = (int)(rest % (unsigned long long)W);
      rest /= (unsigned long long)W;
    }

    unsigned char buf[48];
    buf[0] = '#';
    int plen = 1;
    int over = 0;
    for (int w = 0; w < n; w++) {
      int o = woff[wi[w]], L = wlen[wi[w]];
      if (plen + L > max_name_chars) { over = 1; break; }
      for (int k = 0; k < L; k++) buf[plen + k] = blob[o + k];
      plen += L;
      if (w < n - 1 && ((pattern >> w) & 1u)) {
        if (plen + 1 > max_name_chars) { over = 1; break; }
        buf[plen++] = '-';
      }
    }
    if (over) continue;

    unsigned char d1[32];
    sha256(buf, plen, d1);                 // psk = d1[:16]
    unsigned char d2[32];
    sha256(d1, 16, d2);
    unsigned char hb = d2[0];

    if (wanted[hb]) {
      unsigned char ipad[64], opad[64];
      #pragma unroll
      for (int k = 0; k < 64; k++) {
        unsigned char kb = (k < 16) ? d1[k] : 0;
        ipad[k] = kb ^ 0x36; opad[k] = kb ^ 0x5c;
      }
      Sha c;
      unsigned char innerd[32], macd[32];
      int off = ct_off[hb], clen = ct_len[hb];
      sha_init(&c); sha_update(&c, ipad, 64); sha_update(&c, ct_blob + off, clen); sha_final(&c, innerd);
      sha_init(&c); sha_update(&c, opad, 64); sha_update(&c, innerd, 32); sha_final(&c, macd);
      if (macd[0] == macs[2*hb] && macd[1] == macs[2*hb + 1]) {
        unsigned int slot = atomicAdd(out_cnt, 1u);
        if (slot < out_max) { out_idx[slot] = idx; out_hash[slot] = hb; }
      }
    }
  }
}

}  // extern "C"
"""


_module = None


def _get_module():
    global _module
    if _module is None:
        cp = _load_cupy()
        _module = cp.RawModule(code=_CUDA_SRC, options=("--std=c++11",))
    return _module


def _get_kernel():
    global _kernel
    if _kernel is None:
        _kernel = _get_module().get_function("crack")
    return _kernel


def brute_force_batch_gpu(
    targets,
    charset: str = DEFAULT_CHARSET,
    max_length: int = 6,
    on_progress: Optional[Callable] = None,
    should_stop: Optional[Callable] = None,
    threads_per_block: int = 256,
    chunk_bits: int = 28,
) -> dict:
    """Crack many channels in ONE sweep, amortizing the shared SHA work.

    Args:
        targets: {hash_byte(int): {"mac_and_data": bytes, "extras": [bytes,...]}}.
            One representative packet per channel-hash byte; extras are sibling
            packets used as a collision cross-check during CPU verification.
        others: as brute_force_channel_gpu.

    Returns {hash_byte: "#name"} for every channel solved. Unsolved hashes are
    absent (the caller marks those exhausted). Channels drop out of the sweep as
    they're found, so the wanted set — and the HMAC cost — shrinks over the run.
    """
    cp = _load_cupy()
    if cp is None:
        raise RuntimeError(f"CuPy unavailable: {_import_error}")

    kernel = _get_module().get_function("crack_batch")
    charset_bytes = charset.encode()
    base = len(charset_bytes)
    d_charset = cp.asarray(bytearray(charset_bytes), dtype=cp.uint8)

    wanted = bytearray(256)
    macs = bytearray(512)
    ct_off = [0] * 256
    ct_len = [0] * 256
    ct_parts = []
    off = 0
    active = {}
    for hb, t in targets.items():
        hb = int(hb)
        mad = t["mac_and_data"]
        mac, ct = mad[:2], mad[2:]
        if len(ct) == 0 or len(ct) % CIPHER_BLOCK_SIZE != 0 or len(ct) > 256:
            continue
        wanted[hb] = 1
        macs[2 * hb] = mac[0]
        macs[2 * hb + 1] = mac[1]
        ct_off[hb] = off
        ct_len[hb] = len(ct)
        ct_parts.append(ct)
        off += len(ct)
        active[hb] = t
    if not active:
        return {}

    d_wanted = cp.asarray(bytearray(wanted), dtype=cp.uint8)
    d_macs = cp.asarray(bytearray(macs), dtype=cp.uint8)
    d_ct_off = cp.asarray(ct_off, dtype=cp.int32)
    d_ct_len = cp.asarray(ct_len, dtype=cp.int32)
    d_ct_blob = cp.asarray(bytearray(b"".join(ct_parts) or b"\x00"), dtype=cp.uint8)

    OUT_MAX = 1 << 20
    d_out_idx = cp.zeros(OUT_MAX, dtype=cp.uint64)
    d_out_hash = cp.zeros(OUT_MAX, dtype=cp.uint8)
    d_out_cnt = cp.zeros(1, dtype=cp.uint32)

    solved = {}
    chunk = 1 << chunk_bits
    tpb = threads_per_block
    t0 = time.monotonic()

    for length in range(1, max_length + 1):
        total = base ** length
        pos = 0
        while pos < total:
            if should_stop is not None and should_stop():
                return solved
            if not active:
                return solved
            count = min(chunk, total - pos)
            d_out_cnt.fill(0)
            threads_total = min(int(count), 1 << 22)
            blocks = (threads_total + tpb - 1) // tpb
            kernel(
                (blocks,), (tpb,),
                (d_charset, cp.int32(base), cp.int32(length),
                 cp.uint64(pos), cp.uint64(count), cp.uint64(threads_total),
                 d_wanted, d_macs, d_ct_off, d_ct_len, d_ct_blob,
                 d_out_idx, d_out_hash, d_out_cnt, cp.uint32(OUT_MAX)),
            )
            cnt = int(d_out_cnt.get()[0])
            if cnt:
                n = min(cnt, OUT_MAX)
                idxs = d_out_idx.get()[:n].tolist()
                hbs = d_out_hash.get()[:n].tolist()
                for idx, hb in zip(idxs, hbs):
                    if hb not in active:
                        continue
                    t = active[hb]
                    name = _verify(int(idx), charset_bytes, length,
                                   t["mac_and_data"], tuple(t.get("extras") or ()))
                    if name is not None:
                        solved[hb] = name
                        del active[hb]
                        d_wanted[hb] = 0  # stop testing this hash on-GPU
            pos += count
            if on_progress:
                on_progress(length, pos, time.monotonic() - t0)

    return solved


def _index_to_name(idx: int, charset: bytes, length: int) -> bytes:
    base = len(charset)
    out = bytearray(length)
    n = idx
    for pos in range(length - 1, -1, -1):
        out[pos] = charset[n % base]
        n //= base
    return bytes(out)


def _verify_name(full_name: str, mac_and_data, extra=()):
    """Fully verify a reconstructed "#name" on CPU (shared by charset + words).

    A 2-byte MAC collision over a huge search space can pass the single-packet
    check, so a survivor must (a) decrypt to a strictly-valid GRP_TXT plaintext
    and (b) also MAC-verify every `extra` packet of the same channel. A true
    key decrypts them all; a collision will not.
    """
    ch = Channel.from_hashtag(full_name)
    plaintext = mac_then_decrypt(ch.secret, mac_and_data)
    if plaintext is None or not grp_txt_plaintext_ok(plaintext):
        return None
    # At least one sibling must corroborate (decrypt to valid GRP_TXT). On a
    # shared hash byte the siblings may belong to other channels and simply
    # won't decrypt with this key — that's fine, they just don't corroborate.
    # Callers only brute-force when extras exist, so a lone-packet channel
    # (no corroboration possible) is never brute-forced here.
    if extra:
        if not any(_strict_decodes(ch.secret, e) for e in extra):
            return None
    return full_name


def _verify(idx, charset, length, mac_and_data, extra=()):
    """Reconstruct a charset survivor's name and verify it."""
    name = _index_to_name(idx, charset, length)
    return _verify_name((b"#" + name).decode(), mac_and_data, extra)


def _patterns_for(n: int, include_concat: bool, include_hyphen: bool) -> list:
    """Separator-pattern bitmasks (bit w set => hyphen after word w).

    Both allowed -> every mask 0..2**(n-1)-1; hyphen-only -> all-ones (every gap
    hyphenated); concat-only -> 0. Mirrors multiword._separator_patterns as masks.
    """
    gaps = max(0, n - 1)
    if include_concat and include_hyphen:
        return list(range(1 << gaps))
    if include_hyphen:
        return [(1 << gaps) - 1]
    if include_concat:
        return [0]
    return []


def _words_index_to_name(idx: int, words, n: int, patterns: list, peff: int) -> str:
    """Reconstruct a words survivor's generated name (no '#'), matching the kernel."""
    w = len(words)
    pattern = patterns[idx % peff]
    rest = idx // peff
    wi = [0] * n
    for pos in range(n - 1, -1, -1):
        wi[pos] = rest % w
        rest //= w
    parts = [words[wi[0]]]
    for k in range(1, n):
        if (pattern >> (k - 1)) & 1:
            parts.append("-")
        parts.append(words[wi[k]])
    return "".join(parts)


def brute_force_words_batch_gpu(
    targets,
    words,
    n: int,
    *,
    include_concat: bool = True,
    include_hyphen: bool = True,
    max_name_chars: int = 31,
    on_progress: Optional[Callable] = None,
    should_stop: Optional[Callable] = None,
    threads_per_block: int = 256,
    chunk_bits: int = 28,
) -> dict:
    """Crack many channels in one sweep by combining words from ``words``.

    Candidates are ordered N-tuples of words joined by separator patterns
    (concat and/or hyphen per gap); see :mod:`collector.multiword`. ``words``
    should be the chosen frequency tier (a top-N slice). ``max_name_chars`` is
    the on-device field size incl. the '#'. Same ``targets`` shape and return
    (``{hash_byte: "#name"}``) as :func:`brute_force_batch_gpu`.
    """
    cp = _load_cupy()
    if cp is None:
        raise RuntimeError(f"CuPy unavailable: {_import_error}")

    w = len(words)
    patterns = _patterns_for(n, include_concat, include_hyphen)
    if w == 0 or n < 1 or not patterns:
        return {}
    peff = len(patterns)

    kernel = _get_module().get_function("crack_words")

    # Wordlist blob + offset/length tables on the GPU.
    encoded = [x.encode() for x in words]
    blob = b"".join(encoded)
    offs = []
    lens = []
    o = 0
    for b in encoded:
        offs.append(o)
        lens.append(len(b))
        o += len(b)
    d_blob = cp.asarray(bytearray(blob or b"\x00"), dtype=cp.uint8)
    d_off = cp.asarray(offs, dtype=cp.int32)
    d_len = cp.asarray(lens, dtype=cp.int32)
    d_pat = cp.asarray(patterns, dtype=cp.uint32)

    # Targets -> wanted/macs/ct tables (identical to the charset batch sweep).
    wanted = bytearray(256)
    macs = bytearray(512)
    ct_off = [0] * 256
    ct_len = [0] * 256
    ct_parts = []
    off = 0
    active = {}
    for hb, t in targets.items():
        hb = int(hb)
        mad = t["mac_and_data"]
        mac, ct = mad[:2], mad[2:]
        if len(ct) == 0 or len(ct) % CIPHER_BLOCK_SIZE != 0 or len(ct) > 256:
            continue
        wanted[hb] = 1
        macs[2 * hb] = mac[0]
        macs[2 * hb + 1] = mac[1]
        ct_off[hb] = off
        ct_len[hb] = len(ct)
        ct_parts.append(ct)
        off += len(ct)
        active[hb] = t
    if not active:
        return {}

    d_wanted = cp.asarray(bytearray(wanted), dtype=cp.uint8)
    d_macs = cp.asarray(bytearray(macs), dtype=cp.uint8)
    d_ct_off = cp.asarray(ct_off, dtype=cp.int32)
    d_ct_len = cp.asarray(ct_len, dtype=cp.int32)
    d_ct_blob = cp.asarray(bytearray(b"".join(ct_parts) or b"\x00"), dtype=cp.uint8)

    OUT_MAX = 1 << 20
    d_out_idx = cp.zeros(OUT_MAX, dtype=cp.uint64)
    d_out_hash = cp.zeros(OUT_MAX, dtype=cp.uint8)
    d_out_cnt = cp.zeros(1, dtype=cp.uint32)

    solved = {}
    total = (w ** n) * peff
    chunk = 1 << chunk_bits
    tpb = threads_per_block
    t0 = time.monotonic()

    pos = 0
    while pos < total:
        if should_stop is not None and should_stop():
            return solved
        if not active:
            return solved
        count = min(chunk, total - pos)
        d_out_cnt.fill(0)
        threads_total = min(int(count), 1 << 22)
        blocks = (threads_total + tpb - 1) // tpb
        kernel(
            (blocks,), (tpb,),
            (d_blob, d_off, d_len, cp.int32(w), cp.int32(n),
             d_pat, cp.int32(peff), cp.int32(max_name_chars),
             cp.uint64(pos), cp.uint64(count), cp.uint64(threads_total),
             d_wanted, d_macs, d_ct_off, d_ct_len, d_ct_blob,
             d_out_idx, d_out_hash, d_out_cnt, cp.uint32(OUT_MAX)),
        )
        cnt = int(d_out_cnt.get()[0])
        if cnt:
            m = min(cnt, OUT_MAX)
            idxs = d_out_idx.get()[:m].tolist()
            hbs = d_out_hash.get()[:m].tolist()
            for idx, hb in zip(idxs, hbs):
                if hb not in active:
                    continue
                t = active[hb]
                full = "#" + _words_index_to_name(int(idx), words, n, patterns, peff)
                name = _verify_name(full, t["mac_and_data"], tuple(t.get("extras") or ()))
                if name is not None:
                    solved[hb] = name
                    del active[hb]
                    d_wanted[hb] = 0
        pos += count
        if on_progress:
            on_progress(n, pos, time.monotonic() - t0)

    return solved


def _strict_decodes(secret, mac_and_data) -> bool:
    pt = mac_then_decrypt(secret, mac_and_data)
    return pt is not None and grp_txt_plaintext_ok(pt)


def brute_force_channel_gpu(
    target_hash: int,
    mac_and_data: bytes,
    charset: str = DEFAULT_CHARSET,
    max_length: int = 6,
    on_progress: Optional[Callable] = None,
    extra_mac_and_data=None,
    should_stop: Optional[Callable] = None,
    threads_per_block: int = 256,
    chunk_bits: int = 28,
) -> Optional[str]:
    """GPU port of brute_force.brute_force_channel().

    Args match the CPU version; extra tuning knobs:
        extra_mac_and_data: other packets' [mac][ct] blobs for the same channel;
            a candidate must decrypt all of them (collision guard).
        should_stop: called between kernel launches; return True to abort early
            (returns None), used to cancel an in-flight crack.
        threads_per_block: CUDA block size.
        chunk_bits: indices per kernel launch = 2**chunk_bits (progress/bounding).

    Returns the cracked channel name with '#', or None if exhausted/stopped.
    """
    cp = _load_cupy()
    if cp is None:
        raise RuntimeError(f"CuPy unavailable: {_import_error}")

    mac = mac_and_data[:CIPHER_MAC_SIZE]
    ciphertext = mac_and_data[CIPHER_MAC_SIZE:]
    if len(ciphertext) == 0 or len(ciphertext) % CIPHER_BLOCK_SIZE != 0:
        return None
    if len(ciphertext) > 256:
        # Kernel inner buffer is 64 + 256; real packets are far smaller.
        raise ValueError("ciphertext too long for GPU kernel")

    kernel = _get_kernel()
    charset_bytes = charset.encode()
    base = len(charset_bytes)

    d_charset = cp.asarray(bytearray(charset_bytes), dtype=cp.uint8)
    d_ct = cp.asarray(bytearray(ciphertext), dtype=cp.uint8)

    OUT_MAX = 65536
    d_out_idx = cp.zeros(OUT_MAX, dtype=cp.uint64)
    d_out_cnt = cp.zeros(1, dtype=cp.uint32)

    extra = tuple(extra_mac_and_data or ())

    chunk = 1 << chunk_bits
    tpb = threads_per_block
    t0 = time.monotonic()

    for length in range(1, max_length + 1):
        total = base ** length
        pos = 0
        while pos < total:
            if should_stop is not None and should_stop():
                return None
            count = min(chunk, total - pos)
            d_out_cnt.fill(0)
            # Enough blocks to cover the chunk, capped for grid-stride reuse.
            n_threads = min(count, 1 << 22)
            blocks = (int(n_threads) + tpb - 1) // tpb
            kernel(
                (blocks,), (tpb,),
                (d_charset, cp.int32(base), cp.int32(length),
                 cp.uint64(pos), cp.uint64(count),
                 cp.uint8(target_hash), cp.uint8(mac[0]), cp.uint8(mac[1]),
                 d_ct, cp.int32(len(ciphertext)),
                 d_out_idx, d_out_cnt, cp.uint32(OUT_MAX)),
            )
            cnt = int(d_out_cnt.get()[0])
            if cnt:
                survivors = d_out_idx.get()[:min(cnt, OUT_MAX)]
                for idx in survivors.tolist():
                    name = _verify(int(idx), charset_bytes, length, mac_and_data, extra)
                    if name is not None:
                        return name
            pos += count
            if on_progress:
                on_progress(length, pos, time.monotonic() - t0)

    return None
