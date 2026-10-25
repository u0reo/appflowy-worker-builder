#!/usr/bin/env python3
"""公式 appflowy_worker ELF(ARM64, jemalloc PAGE=4096) を
census(t12/t14 ビルド実測語ペア) + review.txt 台帳 + tc 窓検証で
PAGE=16384 化する。ビルド行為は含まない(解析・書換のみ)。

使い方:
  patch_worker.py EXE --review review.txt --patches patches.json \
      [--tc12 t12 --tc14 t14] [--apply] --out worker_16k.bin \
      --report patch_report.json

decision:
  census    : patches.json の語ペア(実測)を語単位で適用      <- 最有力
  template* : census 無し、静的テンプレ乘算(窓検証済みなら keep)
  keep      : page 非依存と判定(ADDSUBfff/0 変化/窓 tc 両者同一)
  manual    : 判断不能 -> 出力せず人間レビューへ
"""
import argparse
import collections
import json
import os
import re
import struct
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from jem16k import norm, regnorm, dnorm, cmpnorm  # noqa: E402

HDR = re.compile(r'^== (0x[0-9a-f]+)\s+(\S+)\s+(\S+)')
RE_CTX = re.compile(r",\s+#(0x[0-9a-f]+|\d+)")
RE_LABEL = re.compile(r"<[^>]*>")
REGBITS = 0x3FF


def load_phdrs(path):
    """PT_LOAD の (off, vaddr, memsz, flags) 一覧"""
    head = open(path, 'rb').read(64)
    e_phoff, = struct.unpack_from('<Q', head, 0x20)
    e_phentsize, e_phnum = struct.unpack_from('<HH', head, 0x36)
    out = []
    with open(path, 'rb') as f:
        for n in range(e_phnum):
            f.seek(e_phoff + n * e_phentsize)
            h = f.read(e_phentsize)
            typ, fl = struct.unpack_from('<II', h, 0)
            off, vaddr, _pa, _fsz, msz = struct.unpack_from('<QQQQQ', h, 8)
            if typ == 1:
                out.append((off, vaddr, msz, fl))
    return out


def v2o(phdrs, vaddr):
    for off, va, msz, fl in phdrs:
        if va <= vaddr < va + msz:
            return off + (vaddr - va)
    raise SystemExit('vaddr %x はどの PT_LOAD にも無い' % vaddr)


def ctx_norm(t):
    t = RE_LABEL.sub("", t)
    t = RE_CTX.sub(", #?", t)
    if t.replace(" ", "").startswith("adrp"):
        return "adrp"           # adrp 先はデータ配置依存で文脈に不要
    return re.sub(r"\s+", "", t).lower()


def tc_contexts(path):
    """t12/t14 テストバイナリ全 disasm -> {fname: [ctx_norm, ...]}"""
    funcs, cur = {}, None
    for l in os.popen('objdump -d -M no-aliases %s 2>/dev/null' % path):
        m = re.match(r'^[0-9a-f]+ <(.+)>:', l)
        if m:
            cur = m.group(1)
            funcs[cur] = []
        elif cur is not None:
            m = re.match(r'^\s+[0-9a-f]+:\s+[0-9a-f]+\s+(.*)', l)
            if m:
                funcs[cur].append(ctx_norm(m.group(1)))
    return funcs


def window_keep(tc, fname, win):
    """窓(before+site+after。コンテキストの立即値は #? に秘匿するが、
    site 行の #0xK は即値のまま保持)。tc の t12/t14 双方に同一窓が
    存在 -> site 語はビルドページに依らない定数 -> KEEP。
    tc 側で K が変わったなら窓が不一致になり patch 側へ進む。"""
    if not win or fname not in tc[0] or fname not in tc[1]:
        return None
    n = len(win)
    for lst in tc:
        la = lst[fname]
        if not (len(la) >= n and any(la[i:i + n] == win
                                     for i in range(len(la) - n + 1))):
            return None
    return 'tc t12/t14 の窓が同一 -> 非ページ定数'


def load_review(path):
    sites, cur, before, last = [], None, [], None
    for line in open(path, encoding='utf-8'):
        m = HDR.match(line)
        if m:
            cur = (int(m.group(1), 16), m.group(2), m.group(3))
            before, last = [], None
            continue
        if cur is None:
            continue
        s = line.rstrip('\n')
        if s.startswith('>> '):
            last = {'addr': cur[0], 'cat': cur[1], 'func': cur[2],
                    'text': s[3:].split('//')[0].strip(),
                    'before': before[:], 'after': []}
            sites.append(last)
            continue
        st = s.strip()
        if st:
            if last is not None and len(last['after']) < 3:
                last['after'].append(st)
            elif last is None and len(before) < 3:
                before.append(st)
    return sites


def load_census(path):
    d = json.load(open(path))
    m = collections.defaultdict(set)
    for f in d['functions']:
        for s in f.get('sites', []):
            k = regnorm(norm(s['t12']))
            m[k].add((int(s['w12'], 16), int(s['w14'], 16)))
    return {k: sorted(v) for k, v in m.items()}


def try_census(w, samples):
    for (w12, w14) in samples:
        diff = (w12 ^ w14) & ~REGBITS
        if diff == 0:
            continue
        if (w ^ w12) & ~REGBITS:
            continue
        cand = (w & ~diff) | (w14 & diff)
        if cand != w:
            return cand, 'ok'
    return None, 'no-matching-sample'


def load_evid(path):
    """evid_census 出力 JSON -> (changed: key->[(w12,w14)], same: set)"""
    d = json.load(open(path))
    ch = {k: [(int(a, 16), int(b, 16)) for a, b in v]
          for k, v in d['changed'].items()}
    return ch, set(d['same'])


def tc_word_index(t12path, t14path):
    """key -> {page 非依存が実証された t12 ワード}.
    tc の同関数で t12 ワード w が t14 にもそのまま存在 -> w は定数。"""
    import jem16k
    F12 = jem16k.disasm_funcs(t12path)
    F14 = jem16k.disasm_funcs(t14path)
    idx = collections.defaultdict(set)
    for n, f12 in F12.items():
        f14 = F14.get(n)
        if f14 is None:
            continue
        w14s = {x[1] for x in f14}
        for _a, w, t in f12:
            if w in w14s:
                idx[regnorm(cmpnorm(norm(t)))].add(w)
    return idx


def template_xform(w, cat):
    if cat.startswith('ADDSUBx4096'):
        if (w >> 23) & 0x3F == 0b100010 and (w >> 22) & 1:
            imm = (w >> 10) & 0xFFF
            if imm and imm * 4 <= 0xFFF:
                return (w & ~(0xFFF << 10)) | ((imm * 4) << 10), 'template*4'
    elif cat.startswith('MOVZ1000'):
        if (w >> 23) & 0x3F == 0b100101 and not (w >> 17) & 3:
            imm = (w >> 5) & 0xFFFF
            if imm == 0x1000:
                return (w & ~(0xFFFF << 5)) | (0x4000 << 5), 'template*4'
    elif cat == 'MOV12':
        if (w >> 23) & 0x3F == 0b100101 and ((w >> 5) & 0xFFFF) == 0xC:
            return (w & ~(0xFFFF << 5)) | (0xE << 5), 'template+2'
    return None, None



# ---------------------------------------------------------------- ページ意味ルール
# tc ビルド(gcc)が定数畳み込みで跡を持たない関数群を、jemalloc ソース意味論で
# 判定する: MOV x?,#0x1000 -> #0x4000 / lsr x?,#12(ubfm #12,#63) -> #14。
# pages_boot の 0x1000 列は mmap/madvise/munmap の 4KiB プローブ、
# vcprintf/safety_check_fail のそれは出力バッファであり PAGE 独立なので対象外。
PAGE_FUNCS_MOVZ = {
    '_rjem_je_sc_data_init',                 # sc_runch / size2index: PAGE 基準
    '_rjem_je_pages_mark_guards',            # mprotect(base, PAGE, PROT_NONE)
    '_rjem_je_pages_unmark_guards',
    '_rjem_je_cache_bin_info_compute_alloc',  # *max_bin_alignment = PAGE
    '_rjem_je_san_bump_alloc',               # bump 整列 = PAGE
    '_rjem_je_pai_alloc_batch_default',      # alignment = PAGE
    '_rjem_je_ecache_alloc_grow',            # extent alloc alignment = PAGE
}
PAGE_FUNCS_LSR14 = {'_rjem_je_sc_data_init'}  # index = size >> LG_PAGE


def load_syms(path):
    import bisect
    ss = sorted((int(a, 16), n.split()[0])
                for a, n in (l.split(' ', 1) for l in open(path) if l.strip())
                if n.split()[0].startswith('_rjem'))
    return [s[0] for s in ss], ss


def sym_at(addrs, syms, va):
    import bisect
    if va < addrs[0]:
        return None
    i = bisect.bisect_right(addrs, va) - 1
    lo = addrs[i]
    nxt = addrs[i + 1] if i + 1 < len(addrs) else lo + 16
    return syms[i][1] if lo <= va < nxt else None


def pagefuncs_apply(out, raw, ph, report, stats, syms_path):
    """whitelist 関数内の PAGE 語を書き換え、report/stats を再集計する。"""
    addrs, syms = load_syms(syms_path)
    segs = [(va, va + msz) for off, va, msz, fl in ph if fl & 0x4]
    by_addr = {int(r['addr'], 16): r for r in report}
    n_pf = 0
    for lo, hi in segs:
        va = lo + ((-lo) & 3)
        while va < hi - 3:
            name = sym_at(addrs, syms, va)
            if name:
                w, = struct.unpack_from('<I', raw, v2o(ph, va))
                nw = None
                if name in PAGE_FUNCS_MOVZ and (w & 0xFFFFFFE0) == 0xD2820000:
                    nw, why, cat = w + 0x60000, 'pagefunc: PAGE 整列定数', \
                        'MOVZ1000'
                elif name in PAGE_FUNCS_LSR14 and (w & 0xFFFFFC00) == 0xD34CFC00:
                    nw, why, cat = w + 0x20000, 'pagefunc: >>LG_PAGE', 'LSR'
                if nw is not None and nw != w:
                    struct.pack_into('<I', out, v2o(ph, va), nw)
                    rec = by_addr.get(va)
                    if rec is None:
                        rec = {'addr': hex(va), 'cat': cat, 'func': name,
                               'text': '', 'w': hex(w)}
                        report.append(rec)
                        by_addr[va] = rec
                    rec.update(decision='patch', src=why, new=hex(nw))
                    n_pf += 1
            va += 4
    stats.clear()
    for r in report:
        if r['decision'] == 'keep':
            stats['keep'] += 1
        elif r['decision'] == 'manual':
            stats['manual'] += 1
        else:
            stats['patch_' + ('census' if r.get('src') == 'census'
                              else 'pagefunc' if r.get('src', '').startswith(
                                  'pagefunc') else 'template')] += 1
    return n_pf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('exe')
    ap.add_argument('--review', default='/tmp/review.txt')
    ap.add_argument('--patches')
    ap.add_argument('--tc12')
    ap.add_argument('--tc14')
    ap.add_argument('--evid')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--out', default='worker_16k.bin')
    ap.add_argument('--report', default='patch_report.json')
    ap.add_argument('--syms', default='/tmp/syms.txt')
    a = ap.parse_args()

    raw = open(a.exe, 'rb').read()
    ph = load_phdrs(a.exe)

    def rd(v):
        return struct.unpack_from('<I', raw, v2o(ph, v))[0]

    census = load_evid(a.evid)[0] if a.evid else \
        (load_census(a.patches) if a.patches else {})
    tcw = tc_word_index(a.tc12, a.tc14) \
        if a.tc12 and a.tc14 else None
    ev_ch, ev_same = load_evid(a.evid) if a.evid else ({}, set())
    out = bytearray(raw)
    report, stats = [], collections.Counter()

    for s in load_review(a.review):
        addr, cat = s['addr'], s['cat']
        func, txt = s['func'], s['text']
        w = rd(addr)
        key = regnorm(norm(txt))
        rec = {'addr': hex(addr), 'cat': cat, 'func': func,
               'text': txt, 'w': hex(w)}

        def do_template():
            """テンプレ変換 + 非ページ定数検証(evid same / tc 同語実在)。"""
            nw, why = template_xform(w, cat)
            if nw is None:
                return None, None
            if key in ev_same:
                return None, 'evid: tc _align 語が同語(page 非依存)'
            if tcw is not None and w in tcw.get(key, ()):
                return None, 'tc: 同一ワードが t14 にも存在(page 非依存)'
            return nw, why

        if cat == 'ADDSUBfff' or key.startswith('cmn '):
            rec.update(decision='keep',
                       why='cmn #0xfff: census 実測で 0 変化(非ページ由来)')
            stats['keep'] += 1
        elif key in census:
            nw, note = try_census(w, census[key])
            if nw is not None:
                rec.update(decision='patch', src='census', new=hex(nw))
                struct.pack_into('<I', out, v2o(ph, addr), nw)
                stats['patch_census'] += 1
            else:
                nw, why = do_template()
                if nw is not None:
                    rec.update(decision='patch', src=why, new=hex(nw))
                    struct.pack_into('<I', out, v2o(ph, addr), nw)
                    stats['patch_template'] += 1
                elif why:
                    rec.update(decision='keep', why=why)
                    stats['keep'] += 1
                else:
                    rec.update(decision='manual',
                               why='census 語ペアが公式語と非互換: ' + note)
                    stats['manual'] += 1
        else:
            nw, why = do_template()
            if nw is not None:
                rec.update(decision='patch', src=why, new=hex(nw))
                struct.pack_into('<I', out, v2o(ph, addr), nw)
                stats['patch_template'] += 1
            elif why:
                rec.update(decision='keep', why=why)
                stats['keep'] += 1
            else:
                rec.update(decision='manual', why='census にパターン無し')
                stats['manual'] += 1
        report.append(rec)

    n_pf = pagefuncs_apply(out, raw, ph, report, stats, a.syms)
    print('pagefunc sites=%d' % n_pf)
    json.dump({'stats': dict(stats), 'sites': report},
              open(a.report, 'w'), ensure_ascii=False, indent=1)
    print('sites=%d' % len(report), dict(stats))
    if stats['manual']:
        print('!! manual 残 %d: 人間レビューが必要' % stats['manual'])
    if a.apply:
        with open(a.out, 'wb') as f:
            f.write(out)
        print('wrote', a.out)


if __name__ == '__main__':
    main()
