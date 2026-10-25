#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jemalloc 16K ページ対応パッチ解析ツール (aarch64 / jemalloc 5.3.x)

  fp    TARGET [--filter _rjem]  : 関数単位の語レベル指紋 (リロケ依存フィールドを
                                   マスクした 32bit 語列の SHA1)。objdump 非依存なので
                                   binutils の版本が異なる環境間でも比較できる。
                                   TARGET = ELF exe / ビルドツリー dir / .o
  diff  B12 B14 [--filter S]     : lg-page=12/14 ビルドツリー間の命令差分 JSON
                                   (同一コンテナ内の objdump で実行すること)
  match OFFICIAL_FP FP12 [..]    : 公式 exe と各ビルドの関数指紋一致率 (%)
"""
import argparse, glob, hashlib, json, os, re, struct, subprocess, sys
from difflib import SequenceMatcher

RE_HDR = re.compile(r'^([0-9a-f]+) <(.+)>:$')
RE_BRANCH = re.compile(r'^(bl|b(\.[a-z0-9]+)?|cbz|cbnz|tbz|tbnz)$')
RE_HEXADDR = re.compile(r'(?<![#])0x[0-9a-f]{4,}')
RE_IMMS = re.compile(r'#(0x)?[0-9a-f]+')
RE_REG = re.compile(r'\b([xw]\d{1,2})\b')


def mword(w):
    """アドレス相対フィールドだけマスク (ページ定数・即値・レジスタは保持)"""
    if (w >> 26) & 0x1F == 0x05:                 # B / BL (bit31 で区別)
        return 0xB0000000 | ((w >> 31) & 1)
    t = (w >> 24) & 0xFF
    if t == 0x54:                                # B.cond (cond 4bit 保持)
        return 0xB4000000 | (w & 0x0F)
    if t in (0x36, 0x37, 0xB6, 0xB7):            # TBZ / TBNZ / B.b5
        return 0xC1000000 | (t << 8) | (w & 0x1F)
    if t & 0x9F == 0x90:                         # ADRP
        return 0x90000000 | (w & 0x1F)
    if t & 0x9F == 0x10:                         # ADR
        return 0x10000000 | (w & 0x1F)
    if t in (0x18, 0x19, 0x58, 0x59, 0xD8, 0xD9):  # LDR/LDRSW/PRFM literal
        return 0xC2000000 | (t << 8) | (w & 0x1F)
    return w


# ------------------------------------------------------------ 正規化 (diff 用)
def _txt(s):
    return re.sub(r'\s+', ' ', s.split('//')[0]).strip()


def norm(t):
    t = _txt(t).replace(', ', ',')
    m = t.split(None, 1)
    if len(m) < 2:
        return t
    mn, ops = m[0].lower(), m[1]
    if mn == 'adrp':
        ops = ops.split(',')[0]
    elif RE_BRANCH.match(mn) and re.fullmatch(r'(0x)?[0-9a-f]{1,16}', ops.split()[0]):
        ops = '<t>'
    else:
        ops = RE_HEXADDR.sub('0x?', ops)
    return mn + ' ' + ops


def dnorm(t):
    return RE_IMMS.sub('#?', norm(t))


def regnorm(t):
    mp = {}

    def rep(mm):
        r = mm.group(1)
        if r not in mp:
            mp[r] = 'r%d' % len(mp)
        return mp[r]
    return RE_REG.sub(rep, t)


# ------------------------------------------------------------ ELF / nm
class Elf:
    def __init__(self, path):
        self.d = d = open(path, 'rb').read()
        assert d[:4] == b'\x7fELF' and d[4] == 2, path + ': ELF64 のみ対応'
        shoff = struct.unpack_from('<Q', d, 0x28)[0]
        shentsize = struct.unpack_from('<H', d, 0x3a)[0]
        shnum = struct.unpack_from('<H', d, 0x3c)[0]
        shstrndx = struct.unpack_from('<H', d, 0x3e)[0]
        self.shstr_off = struct.unpack_from('<Q', d, shoff + shstrndx * shentsize + 24)[0]
        self.secs = []
        for i in range(shnum):
            o = shoff + i * shentsize
            name, stype = struct.unpack_from('<II', d, o)
            addr = struct.unpack_from('<Q', d, o + 16)[0]
            off = struct.unpack_from('<Q', d, o + 24)[0]
            size = struct.unpack_from('<Q', d, o + 32)[0]
            self.secs.append((name, stype, addr, off, size))

    def text(self):
        for name, stype, addr, off, size in self.secs:
            if stype != 1:
                continue
            z = self.d.index(b'\0', self.shstr_off + name)
            if self.d[self.shstr_off + name:z] == b'.text':
                return bytes(self.d[off:off + size]), addr
        raise SystemExit('.text section が見つかりません: ' + self.path)


def nm_syms(path, lo, hi):
    out = subprocess.run(['nm', '--defined-only', path], capture_output=True,
                         text=True).stdout
    syms = []
    for l in out.splitlines():
        p = l.split()
        if len(p) != 3 or p[1] not in 'Tt':
            continue
        try:
            a = int(p[0], 16)
        except ValueError:
            continue
        if lo <= a < hi:
            syms.append((a, p[2]))
    syms.sort()
    return syms


def func_spans(syms, base, end):
    out, addrs = [], [s[0] for s in syms]
    for i, (a, n) in enumerate(syms):
        stop = addrs[i + 1] if i + 1 < len(syms) else end
        if stop > a:
            out.append((n, a - base, stop - a))
    return out


# ------------------------------------------------------------ fp
def cmd_fp(a):
    if os.path.isdir(a.target):
        roots = [f for f in sorted(glob.glob(os.path.join(a.target, 'src', '*.o')))
                 if not f.endswith('.pic.o')]
    else:
        roots = [a.target]
    n = 0
    for root in roots:
        e = Elf(root)
        e.path = root
        data, base = e.text()
        for name, off, size in func_spans(nm_syms(root, base, base + len(data)),
                                          base, base + len(data)):
            if a.filter and a.filter not in name:
                continue
            words = struct.unpack_from('<%dI' % (size // 4), data, off)
            h = hashlib.sha1(b''.join(struct.pack('<I', mword(w)) for w in words))
            key = name if len(roots) == 1 else f'{name}@{os.path.basename(root)}'
            print('%s %s %d' % (h.hexdigest()[:12], key, size // 4))
            n += 1
    if n == 0:
        raise SystemExit('関数 0 件')
    return 0


# ------------------------------------------------------------ diff
def iter_funcs(args):
    cur, ins = None, []
    p = subprocess.Popen(['objdump', '-d'] + args, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    for line in p.stdout:
        s = line.strip()
        m = RE_HDR.match(s)
        if m:
            if cur is not None:
                yield cur, ins
            cur, ins = m.group(2), []
            continue
        if cur is None:
            continue
        t = s.split()
        if len(t) >= 2 and t[0].endswith(':') and re.fullmatch(r'[0-9a-f]{8}', t[1]):
            ins.append((int(t[0][:-1], 16), int(t[1], 16), _txt(' '.join(t[2:]))))
    p.stdout.close()
    p.wait()
    if cur is not None:
        yield cur, ins


def func_map_objs(td, filt):
    d = {}
    for f in sorted(glob.glob(os.path.join(td, 'src', '*.o'))):
        if f.endswith('.pic.o'):
            continue
        for name, ins in iter_funcs([f]):
            if filt and filt not in name:
                continue
            d[name] = ins
    return d


def cmd_diff(a):
    A, B = func_map_objs(a.b12, a.filter), func_map_objs(a.b14, a.filter)
    names = sorted(n for n in A if n in B)
    tot = {'funcs': len(names), 'identical': 0, 'changed_funcs': 0,
           'exact': 0, 'renamed': 0, 'ambig': 0,
           'missing_in14': sorted(n for n in A if n not in B),
           'unaligned_funcs': []}
    census, funcs = {}, []
    for n in names:
        ia, ib = A[n], B[n]
        fa, fb = [x[2] for x in ia], [x[2] for x in ib]
        na, nb = [dnorm(x) for x in fa], [dnorm(x) for x in fb]
        strict = len(fa) == len(fb) and na == nb
        pairs = (list(enumerate(range(len(fa)))) if strict else
                 [(x, y) for blk in SequenceMatcher(None, na, nb, autojunk=False).get_matching_blocks()
                  for x, y in zip(range(blk.a, blk.a + blk.size), range(blk.b, blk.b + blk.size))])
        sites = []
        for i, j in pairs:
            if ia[i][1] == ib[j][1]:
                continue
            if regnorm(fa[i]) == regnorm(fb[j]):
                conf = 'exact'
            elif regnorm(dnorm(fa[i])) == regnorm(dnorm(fb[j])):
                conf = 'renamed'
            else:
                conf = 'ambig'
            sites.append({'i': i, 'addr': hex(ia[i][0]),
                          'w12': '%08x' % ia[i][1], 't12': fa[i],
                          'w14': '%08x' % ib[j][1], 't14': fb[j],
                          'pat': regnorm(norm(fa[i])) + ' => ' + regnorm(norm(fb[j])),
                          'conf': conf})
        if not sites:
            if strict:
                tot['identical'] += 1
            else:
                tot['unaligned_funcs'].append(n)
            continue
        tot['changed_funcs'] += 1
        if not strict:
            tot['unaligned_funcs'].append(n)
        for s in sites:
            tot[s['conf']] += 1
            census[s['pat']] = census.get(s['pat'], 0) + 1
        funcs.append({'name': n, 'len12': len(ia), 'len14': len(ib),
                      'strict': strict, 'changed': len(sites), 'sites': sites})
    tot['pattern_census'] = dict(sorted(census.items(), key=lambda kv: -kv[1])[:80])
    json.dump({'toolchain': os.environ.get('TAG', 'unknown'),
               'note': 'exact=語がレジスタ含む同一(masked のみ差), renamed=レジスタ割当差のみ,'
                       ' ambig/unaligned=命令列構造差=パターンの手で検証が必要',
               'totals': tot, 'functions': funcs}, sys.stdout)
    print()
    return 0


# ------------------------------------------------------------ match
def load_fp(f):
    d = {}
    for l in open(f):
        p = l.split()
        if len(p) >= 2:
            d.setdefault(p[1].split('@')[0], p[0])
    return d


def cmd_match(a):
    off = load_fp(a.official)
    if not off:
        raise SystemExit('official fp が空')
    for f in a.fp12:
        o = load_fp(f)
        common = [k for k in o if k in off]
        same = sum(1 for k in common if o[k] == off[k])
        print('%-44s common %5d/%5d  byte-identical %5d  match %5.1f%%'
              % (f, len(common), len(off), same, 100.0 * same / len(off)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('fp')
    s.add_argument('target'); s.add_argument('--filter', default='_rjem')
    s.set_defaults(fn=cmd_fp)
    s = sub.add_parser('diff')
    s.add_argument('b12'); s.add_argument('b14'); s.add_argument('--filter', default='_rjem')
    s.set_defaults(fn=cmd_diff)
    s = sub.add_parser('match')
    s.add_argument('official'); s.add_argument('fp12', nargs='+')
    s.set_defaults(fn=cmd_match)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == '__main__':
    main()
