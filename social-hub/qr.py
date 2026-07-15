"""Pure-stdlib QR code encoder + terminal renderer.

Villa is deliberately dependency-free, so we generate the "scan to open on
your phone" QR ourselves rather than pulling in a library. This is a faithful
QR Code Model 2 encoder: byte mode, error-correction level L (max data
density — a URL is not safety-critical), versions 1-10 auto-selected by
payload length, full Reed-Solomon ECC over GF(256), BCH format info, and all
eight data masks scored by the standard penalty rules.

Public API:
    encode(text)      -> list[list[bool]]   the module matrix (True = dark)
    render_ansi(text) -> str                a scannable half-block rendering

Rendering prints LIGHT modules as lit blocks on a dark terminal, which is the
orientation phone cameras expect (a normal QR is dark-on-light; on a dark
terminal we invert so the quiet zone and light modules are the bright ones).
"""
from __future__ import annotations

# ── GF(256) arithmetic for Reed-Solomon (primitive poly 0x11d, generator 2) ──
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """Generator polynomial for `degree` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        # multiply poly by (x - a^i)
        new = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            new[j] ^= _gf_mul(coef, 1)
            new[j + 1] ^= _gf_mul(coef, _EXP[i])
        poly = new
    return poly


def _rs_ecc(data: list[int], degree: int) -> list[int]:
    """Compute `degree` Reed-Solomon EC codewords for `data`."""
    gen = _rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(degree):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


# ── Per-version block structure for ECC level L ──────────────────────────────
# (ec_per_block, [(num_blocks, data_codewords_per_block), ...]), remainder bits.
# Data-codeword capacity in byte mode for the terminator/pad step is the sum of
# all blocks' data codewords.
_VERSION_L: dict[int, tuple[int, list[tuple[int, int]]]] = {
    1: (7, [(1, 19)]),
    2: (10, [(1, 34)]),
    3: (15, [(1, 55)]),
    4: (20, [(1, 80)]),
    5: (26, [(1, 108)]),
    6: (18, [(2, 68)]),
    7: (20, [(2, 78)]),
    8: (24, [(2, 97)]),
    9: (30, [(2, 116)]),
    10: (18, [(2, 68), (2, 69)]),
}
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

# Byte-mode data capacity (codewords) per version at ECC L.
_DATA_CODEWORDS = {v: sum(n * d for n, d in blocks) for v, (_, blocks) in _VERSION_L.items()}

# Alignment-pattern centre coordinates per version (v1 has none).
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}


def _pick_version(nbytes: int) -> int:
    for v in range(1, 11):
        # mode(4) + count(8|16) + data(8n) must fit in data-codeword bits.
        count_bits = 16 if v >= 10 else 8
        need_bits = 4 + count_bits + 8 * nbytes
        if need_bits <= _DATA_CODEWORDS[v] * 8:
            return v
    raise ValueError("payload too long for QR versions 1-10 (max ~271 bytes)")


def _make_bitstream(data: bytes, version: int) -> list[int]:
    """Byte-mode bit stream → padded data codewords for `version`."""
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    count_bits = 16 if version >= 10 else 8
    put(0b0100, 4)              # byte mode
    put(len(data), count_bits)  # character count
    for byte in data:
        put(byte, 8)

    cap_bits = _DATA_CODEWORDS[version] * 8
    # Terminator: up to four zero bits, only as many as fit.
    put(0, min(4, cap_bits - len(bits)))
    # Pad to a byte boundary.
    if len(bits) % 8:
        put(0, 8 - (len(bits) % 8))
    # Codewords, then pad bytes alternating 0xEC / 0x11 — the FIRST pad byte
    # is always 0xEC, so key the alternation off the pad index, not the total.
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    pad_i = 0
    while len(codewords) < _DATA_CODEWORDS[version]:
        codewords.append(pad[pad_i % 2])
        pad_i += 1
    return codewords


def _interleave(codewords: list[int], version: int) -> list[int]:
    """Split data into blocks, append per-block EC, interleave both."""
    ec_per_block, groups = _VERSION_L[version]
    blocks: list[list[int]] = []
    idx = 0
    for num, dlen in groups:
        for _ in range(num):
            blocks.append(codewords[idx:idx + dlen])
            idx += dlen
    ecs = [_rs_ecc(b, ec_per_block) for b in blocks]

    result: list[int] = []
    maxlen = max(len(b) for b in blocks)
    for i in range(maxlen):
        for b in blocks:
            if i < len(b):
                result.append(b[i])
    for i in range(ec_per_block):
        for e in ecs:
            result.append(e[i])
    return result


def _codewords_to_bits(codewords: list[int], version: int) -> list[int]:
    bits: list[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    bits.extend([0] * _REMAINDER_BITS[version])
    return bits


# ── Matrix construction ──────────────────────────────────────────────────────
class _Matrix:
    def __init__(self, size: int) -> None:
        self.size = size
        self.mod = [[0] * size for _ in range(size)]        # 0 light / 1 dark
        self.reserved = [[False] * size for _ in range(size)]

    def set(self, r: int, c: int, dark: int, reserve: bool = True) -> None:
        self.mod[r][c] = 1 if dark else 0
        if reserve:
            self.reserved[r][c] = True


def _place_finder(m: _Matrix, r: int, c: int) -> None:
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < m.size and 0 <= cc < m.size):
                continue
            if dr in (-1, 7) or dc in (-1, 7):
                dark = 0  # separator ring
            elif dr in (0, 6) or dc in (0, 6):
                dark = 1
            elif 2 <= dr <= 4 and 2 <= dc <= 4:
                dark = 1
            else:
                dark = 0
            m.set(rr, cc, dark)


def _place_alignment(m: _Matrix, version: int) -> None:
    centres = _ALIGN[version]
    for r in centres:
        for c in centres:
            # Skip the three finder-pattern corners.
            if (r <= 8 and c <= 8) or (r <= 8 and c >= m.size - 9) or (r >= m.size - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    ring = max(abs(dr), abs(dc))
                    m.set(r + dr, c + dc, 1 if ring in (0, 2) else 0)


def _place_timing(m: _Matrix) -> None:
    for i in range(8, m.size - 8):
        bit = 1 if i % 2 == 0 else 0
        if not m.reserved[6][i]:
            m.set(6, i, bit)
        if not m.reserved[i][6]:
            m.set(i, 6, bit)


def _reserve_format(m: _Matrix) -> None:
    n = m.size
    for i in range(9):
        if i != 6:
            m.reserved[8][i] = True
            m.reserved[i][8] = True
    for i in range(8):
        m.reserved[8][n - 1 - i] = True
        m.reserved[n - 1 - i][8] = True
    m.set(n - 8, 8, 1)  # always-dark module


def _reserve_version(m: _Matrix, version: int) -> None:
    if version < 7:
        return
    n = m.size
    for i in range(6):
        for j in range(3):
            m.reserved[i][n - 11 + j] = True
            m.reserved[n - 11 + j][i] = True


_FORMAT_MASK = 0b101010000010010


def _format_bits(mask: int) -> int:
    """15-bit BCH format info for ECC level L + mask."""
    data = (0b01 << 3) | mask      # L = 01
    v = data << 10
    g = 0b10100110111
    for i in range(4, -1, -1):
        if v & (1 << (i + 10)):
            v ^= g << i
    return ((data << 10) | v) ^ _FORMAT_MASK


def _place_format(m: _Matrix, mask: int) -> None:
    bits = _format_bits(mask)
    n = m.size
    # Format bits are placed MSB-first: position 0 holds bit 14.
    def fbit(pos: int) -> int:
        return (bits >> (14 - pos)) & 1
    # Copy 1: around the top-left finder.
    for i in range(15):
        bit = fbit(i)
        if i < 6:
            m.set(8, i, bit)
        elif i == 6:
            m.set(8, 7, bit)
        elif i == 7:
            m.set(8, 8, bit)
        elif i == 8:
            m.set(7, 8, bit)
        else:
            m.set(14 - i, 8, bit)
    # Copy 2: 7 cells up column 8 from the bottom (positions 0-6), then 8
    # cells along row 8 from the right (positions 7-14). Position 7 must NOT
    # land on (n-8, 8) — that's the always-dark module, set separately.
    for i in range(15):
        bit = fbit(i)
        if i < 7:
            m.set(n - 1 - i, 8, bit)
        else:
            m.set(8, n - 15 + i, bit)


def _place_version(m: _Matrix, version: int) -> None:
    if version < 7:
        return
    v = version
    g = 0b1111100100101
    d = v << 12
    for i in range(5, -1, -1):
        if d & (1 << (i + 12)):
            d ^= g << i
    bits = (v << 12) | d
    n = m.size
    for i in range(18):
        bit = (bits >> i) & 1
        a, b = i // 3, i % 3
        m.set(n - 11 + b, a, bit)
        m.set(a, n - 11 + b, bit)


def _place_data(m: _Matrix, bits: list[int]) -> None:
    n = m.size
    idx = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:      # skip the vertical timing column
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for row in rows:
            for c in (col, col - 1):
                if not m.reserved[row][c]:
                    bit = bits[idx] if idx < len(bits) else 0
                    m.mod[row][c] = bit
                    idx += 1
        upward = not upward
        col -= 2


def _mask_fn(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def _apply_mask(m: _Matrix, mask: int) -> _Matrix:
    out = _Matrix(m.size)
    out.mod = [row[:] for row in m.mod]
    out.reserved = [row[:] for row in m.reserved]
    for r in range(m.size):
        for c in range(m.size):
            if not m.reserved[r][c] and _mask_fn(mask, r, c):
                out.mod[r][c] ^= 1
    return out


def _penalty(m: _Matrix) -> int:
    n, mod, score = m.size, m.mod, 0
    # Rule 1: runs of 5+ same-colour modules in rows and columns.
    for i in range(n):
        for line in (mod[i], [mod[r][i] for r in range(n)]):
            run, prev = 1, line[0]
            for v in line[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run, prev = 1, v
            if run >= 5:
                score += 3 + (run - 5)
    # Rule 2: 2x2 blocks of the same colour.
    for r in range(n - 1):
        for c in range(n - 1):
            if mod[r][c] == mod[r][c + 1] == mod[r + 1][c] == mod[r + 1][c + 1]:
                score += 3
    # Rule 3: finder-like 1:1:3:1:1 patterns in rows and columns.
    pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for i in range(n):
        row = mod[i]
        colv = [mod[r][i] for r in range(n)]
        for line in (row, colv):
            for j in range(n - 10):
                seg = line[j:j + 11]
                if seg == pat_a or seg == pat_b:
                    score += 40
    # Rule 4: overall dark/light balance.
    dark = sum(sum(row) for row in mod)
    ratio = dark * 100 // (n * n)
    score += 10 * (min(abs(ratio - 50) // 5, 100))
    return score


def _build(text: str, force_mask: int | None = None) -> tuple[list[list[int]], int, int]:
    data = text.encode("utf-8")
    version = _pick_version(len(data))
    codewords = _make_bitstream(data, version)
    final = _interleave(codewords, version)
    bits = _codewords_to_bits(final, version)

    base = _Matrix(17 + 4 * version)
    _place_finder(base, 0, 0)
    _place_finder(base, 0, base.size - 7)
    _place_finder(base, base.size - 7, 0)
    _place_alignment(base, version)
    _reserve_format(base)
    _reserve_version(base, version)
    _place_timing(base)
    _place_data(base, bits)

    masks = [force_mask] if force_mask is not None else range(8)
    best, best_score, best_mask = None, None, 0
    for mask in masks:
        cand = _apply_mask(base, mask)
        _place_format(cand, mask)
        _place_version(cand, version)
        s = _penalty(cand)
        if best_score is None or s < best_score:
            best, best_score, best_mask = cand, s, mask
    return best.mod, version, best_mask


def encode(text: str) -> list[list[bool]]:
    """Return the QR module matrix for `text` (True = dark module)."""
    mod, _, _ = _build(text)
    return [[bool(v) for v in row] for row in mod]


def render_ansi(text: str, *, quiet_zone: int = 2) -> str:
    """Render `text` as a scannable QR using half-block characters.

    Two matrix rows share one terminal line via ▀/▄/█/space. On a dark
    terminal we print LIGHT modules as bright blocks (inverted), which phone
    cameras read as a standard dark-on-light code.
    """
    mod = encode(text)
    n = len(mod)
    q = quiet_zone
    size = n + 2 * q

    def dark(r: int, c: int) -> bool:
        rr, cc = r - q, c - q
        if 0 <= rr < n and 0 <= cc < n:
            return mod[rr][cc]
        return False  # quiet zone is light

    lines = []
    for r in range(0, size, 2):
        out = []
        for c in range(size):
            top_light = not dark(r, c)
            bot_light = (r + 1 < size) and not dark(r + 1, c)
            # light = lit block (inverted for dark terminals)
            if top_light and bot_light:
                out.append("█")   # full block
            elif top_light and not bot_light:
                out.append("▀")   # upper half
            elif not top_light and bot_light:
                out.append("▄")   # lower half
            else:
                out.append(" ")
        lines.append("".join(out))
    return "\n".join(lines)


def render_svg(
    text: str,
    *,
    quiet_zone: int = 4,
    dark: str = "#0b0b0d",
    light: str = "#ffffff",
) -> str:
    """Render `text` as a self-contained SVG QR — dark modules on a light
    background, which is what phone cameras expect regardless of the page
    theme. The viewBox is in module units so the caller scales it purely with
    CSS (`width: …`). Adjacent dark modules are merged into horizontal runs to
    keep the node count small.
    """
    mod = encode(text)
    n = len(mod)
    q = quiet_zone
    size = n + 2 * q

    rects: list[str] = []
    for r in range(n):
        row = mod[r]
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                rects.append(
                    f'<rect x="{start + q}" y="{r + q}" width="{c - start}" height="1"/>'
                )
            else:
                c += 1

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{size}" height="{size}" fill="{light}"/>'
        f'<g fill="{dark}">{"".join(rects)}</g>'
        f"</svg>"
    )


__all__ = ["encode", "render_ansi", "render_svg"]
