#!/usr/bin/env python3
"""wscan_worker.py — WORD 全走査 census patcher (最終機構).

review.txt 式スキャン(特定定数家族)では jemalloc 構造体の
「アレイサイズ由来 ldr/str オフセット」(例: ldr x1,[x1,#25648])を
拾えず、base ポインタ取得が がらみ値になって SIGSEGV した。

tc テンプレ(t12/t14)の word 差分 census(evid.json)を
対象 exe の全命令 word へ適用する:

  1) exact語一致: 対象 w が census の w12 と完全一致 -> w14 へ。
     同一エンコーディング = 同一命令(レジスタ込み)なので意味保証は最大化。
  2) diffマスク写像(限定): try_census と同一式
        (w & ~diff) | (w14 & diff),  diff=(w12^w14)&~REGBITS
     を、キー文本に定数が十進可視な family(ld*/st* の [rN,#dec] と
     ubfm/sbfm/bfm alias)に限定して適用。
     16進即値は norm で 0x? に潰れ layout 差分と混線するため対象外。

安全機構:
  * b/bl/blr/br/b.xx/cb?/tb?/adr/adrp/[pc]リテラル は layout 専用 -> 全除外
  * exec(命令) LOAD のアライン word のみ
  * 多重適用防止: w が census の変換後語(w14)集合にあれば素通し
  * key が same(ページ非依存実測)なら (2) をスキップ
usage:
  python3 wscan_worker.py --in IN --out OUT --evid /tmp/evid.json \
      --report /tmp/wscan.json --apply
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jem16k import norm, regnorm
REGBITS = 0x3FF

RE_BRANCHISH = re.compile(
    r'^(b|bl|br|blr|b\.\w+|cbnz|cbz|tbnz|tbz|adr|adrp)\b')
DEC_PREFIXES = ('ldr', 'str', 'ldrb', 'strb', 'ldrh', 'strh', 'ldrsb',
                'ldrsh', 'sxt', 'ldursw', 'ldur', 'stur', 'ldp', 'stp',
                'ldnp', 'stnp', 'ubfm', 'sbfm', 'bfm', 'lsr', 'lsl',
                'asr', 'ror', 'fmov')


REGBITS_ = 0x3FF


def load_words(path):
    """objdump -d の全語集合 (ground truth 検証用)."""
    import subprocess as sp
    out = sp.run(['objdump', '-d', '-M', 'no-aliases', path],
                 stdout=sp.PIPE, text=True).stdout
    ws = set()
    for line in out.splitlines():
        mm = re.match(r'^\s+([0-9a-f]+):\s+([0-9a-f]{8})\s', line)
        if mm:
            ws.add(int(mm.group(2), 16))
    return ws


def pair_ok(txt, w12, w14):
    """census ペア w12->w14 がページサイズ依存の派生か。
    tc 側のレイアウト伝播(フレーム offset 等)は worker では毒になるため、
    Δ=16384-4096=0x3000 の倍数だけを通す。"""
    mn = txt.split(None, 1)[0].lower().rstrip('#')
    if mn in ('mov', 'movz', 'movk'):
        i12, i14 = (w12 >> 5) & 0xFFFF, (w14 >> 5) & 0xFFFF
        return i12 == 0x1000 and i14 == 0x4000
    if mn in ('ubfm', 'sbfm', 'bfm', 'asr', 'lshr', 'lsl', 'ubfx', 'sbfx'):
        s12, s14 = (w12 >> 16) & 0x3F, (w14 >> 16) & 0x3F
        return s12 in (12, 13) and s14 == 14
    # ld*/st*/ldp/stp: 任意 offset 移動を許容 (t14 ground truth が検証)
    return True


def dec_key_ok(key):
    if re.search(r'\[\w+,\s*#\d+\]', key):
        return True
    if re.match(r'^(ubfm|sbfm|bfm|asr|lsl|lsr|ror)\b', key) \
            and '#' in key and '0x?' not in key:
        return True
    return False


def load_phdrs(path):
    out = subprocess.run(['readelf', '-lW', path], stdout=subprocess.PIPE,
                         check=True).stdout.decode()
    ph = []
    for line in out.splitlines():
        mm = re.match(r'\s+LOAD\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+0x[0-9a-f]+'
                      r'\s+0x([0-9a-f]+)\s+0x[0-9a-f]+\s+([RWE ]{3})', line)
        if mm:
            ph.append((int(mm.group(1), 16), int(mm.group(2), 16),
                       int(mm.group(3), 16), mm.group(4)))
    return [p for p in ph if 'E' in p[3]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--evid', default='/tmp/evid.json')
    ap.add_argument('--in', dest='inp', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--report', default='/tmp/wscan.json')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    d = json.load(open(a.evid))
    changed = {k: [(int(x, 16), int(y, 16)) for x, y in v]
               for k, v in d['changed'].items()}
    same = set(d['same'])

    wmap, done_words = {}, set()
    for k, pairs in changed.items():
        if RE_BRANCHISH.match(k):
            continue
        for (w12, w14) in pairs:
            if w12 != w14:
                if pair_ok(k, w12, w14):
                    wmap.setdefault(w12, set()).add(w14)
                done_words.add(w14)
    dec_keys = {k: v for k, v in changed.items()
                if dec_key_ok(k) and not RE_BRANCHISH.match(k)}
    # census に現れる関数(_rjem_*)のみが適用域 (= tc で実際に変化した
    # 関数だけ)。他オブジェクトの同一語を page 誤変換しないための必須鎖。
    import bisect
    # /tmp/syms_t3.txt: objdump -t から生成した addr size name (実サイズ)
    try:
        ROWS = [(int(q[0], 16), int(q[1], 16))
                for q in (ln.split() for ln in open('/tmp/syms_t3.txt'))
                if len(q) == 3 and q[2].startswith('_rjem')]
    except FileNotFoundError:
        raise SystemExit('/tmp/syms_t3.txt が必要 '
                         '(objdump -t + _rjem 抽出, addr size name)')
    ROWS.sort()
    sa2 = [a for a, _ in ROWS]
    szv = [z for _, z in ROWS]

    def in_jemalloc(va):
        i = bisect.bisect_right(sa2, va) - 1
        return i >= 0 and va < ends[i]

    ph = load_phdrs(a.inp)
    ends = [a + z if z else sa2[i + 1] if i + 1 < len(sa2) else a + 1
            for i, (a, z) in enumerate(ROWS)]
    buf = bytearray(open(a.inp, 'rb').read())

    line_re = re.compile(r'^\s+([0-9a-f]+):\s+([0-9a-f]{8})\s+(.*)$')
    patched = []
    guard_done = guard_same = guard_w14 = 0
    W14 = load_words('/tmp/tc14/t14')
    proc = subprocess.Popen(['objdump', '-d', '-M', 'no-aliases', a.inp],
                            stdout=subprocess.PIPE, text=True,
                            bufsize=1 << 20)
    for line in proc.stdout:
        mm = line_re.match(line)
        if not mm:
            continue
        va = int(mm.group(1), 16)
        w = int(mm.group(2), 16)
        txt = mm.group(3).strip()
        o = None
        for off, va0, fsz, _p in ph:
            if va0 <= va < va0 + fsz:
                o = off + (va - va0)
                break
        if o is None:
            continue
        if not in_jemalloc(va):
            continue
        cand = src = None
        cands = wmap.get(w)
        if cands and len(cands) == 1:
            cand = next(iter(cands))
            src = 'exact'
        else:
            mn = txt.split(None, 1)[0].lower() if txt else ''
            if mn.startswith(DEC_PREFIXES) and ',' in txt and '#' in txt:
                key = regnorm(norm(txt))
                if RE_BRANCHISH.match(key):
                    continue
                if key in dec_keys:
                    if key in same:
                        guard_same += 1
                        continue
                    for (w12, w14) in dec_keys[key]:
                        diff = (w12 ^ w14) & ~REGBITS
                        if diff == 0 or (w ^ w12) & ~REGBITS:
                            continue
                        c2 = (w & ~diff) | (w14 & diff)
                        if c2 != w:
                            if pair_ok(txt, w, c2):
                                cand, src = c2, 'diff'
                            break
        if cand is not None and cand not in W14:
            cand = None
            guard_w14 += 1
        if cand is None or cand == w:
            continue
        if w in done_words:
            guard_done += 1
            continue
        struct.pack_into('<I', buf, o, cand)
        patched.append({'addr': hex(va), 'src': src, 'text': txt,
                        'w': hex(w), 'new': hex(cand)})
    proc.wait()

    json.dump({'patched': patched, 'guard_done': guard_done,
               'guard_same': guard_same}, open(a.report, 'w'),
              ensure_ascii=False)
    print('wscan patched=%d (guard_done=%d guard_same=%d, wmap_words=%d)'
          % (len(patched), guard_done, guard_same, len(wmap)))
    from collections import Counter
    for k, n in Counter(p['text'].split()[0] for p in patched).most_common(12):
        print('  %-8s %d' % (k, n))
    if a.apply:
        open(a.out, 'wb').write(buf)
        print('wrote', a.out)
    else:
        print('(dry-run: --apply で書込)')


if __name__ == '__main__':
    main()
