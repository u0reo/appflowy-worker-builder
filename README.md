# appflowy-worker-builder — 公式 appflowy_worker の 16K ページ対応バイナリパッチ

Raspberry Pi OS (2025-05+ カーネル 6.12.x rpi = **16K ページ**) で AppFlowy
Cloud を自建すると公式 `appflowyinc/appflowy_worker` が起動しない。本リポジトリは
**公式バイナリをバイナリパッチして 16K ページ対応版を作る** (B 案) ための
調査・分析・ビルド CI 群。現状: **GO 判定済み、パッチ実装フェーズ**。

## 症状と原因

```
<jemalloc>: Unsupported system page size   ← worker コンテナ起動直後に落ちてこれを出す
```

- 公式 worker _staticリンク_の jemalloc が `--with-lg-page=12`
  (PAGE=4096) でビルドされている
- jemalloc は `pages_boot()` で `システムページ > ビルド時ページ` を検出すると
  意図的に abort (自己防衛)。機能不全ではない
- aarch64 の rust-std 既定は `max-regular-page=4096` なので、Rust ビルドでは
  16K 想定ビルドが原理的に作れない → **公式イメージを出さないことには始まらない**
  → 公式 exe への後付けパッチが唯一現実的な経路

## 証明済みの事実 (すべて実測)

| # | 事実 | 証明方法 |
|---|------|---------|
| 1 | 組込み jemalloc は e13ca993 (tikv/jemalloc fork, 5.3.0-1-g...) = tikv-jemalloc-sys 0.6.x の vendored ツリーとバイト同一 | バイナリ内バージョン文字列 + クレート tarball との diff |
| 2 | e13ca993 の jemalloc を `--with-lg-page=14` でビルド→ **16K カーネルの Pi で `OK` rc=0** | CI ビルド成果物 t14 を Pi (6.12.47+rpt-rpi-2712, PAGE_SIZE=16384) で直接実行 |
| 3 | 同款 lg12 ビルド t12 は同じ Pi で公式と同一の `Unsupported system page size` rc=2 | 上と同時対照 |
| 4 | lg14 jemalloc は **4K ホストでも正常動作** (CI の armランナー=4K で t14 OK) | パッチ済み worker は 4K/16K 両対応になることを意味する |
| 5 | 公式 exe と CI 16 変種ビル드의 関数指紋一致率は **1.2〜1.9%** (関数名は 417/417 common) | jem16k.py match (run#6, 全 16 ツールチェーン) |
| 6 | lg12↔lg14 実測 diff のパターン頻度表 (census) はページ定数の写像として整合 | jem16k.py diff patches.json、run#6 tc-* |
| 7 | `cmn x,#0xfff` (公式 26 サイト) は lg14 ビルドでも**一切変化しない** = ページ非由来 | 実測 census に cmn キー 0 件 → **放置が正と確定** |

5 より「ビルド差分の 1:1 移植」は **NO-GO** (公式の codegen はどの CI gcc とも別物)。
判定 **GO** の根拠は 2,6 と下記サイト台帳。

## 判明したパッチ対象の全様相 (公式 exe = ELF aarch64 PIE, jemalloc 節 0x49b0000–0x4a00000)

サイト台帳 `/tmp/review.txt`: 公式内ページ定数参照 **345 サイト**、内訳:

```
71 ADDSUBx4096_K16   65 ADDSUBx4096_sp   49 ADDSUBx4096   29 MOVZ1000
26 ADDSUBfff (cmn #0xfff — 不変で正しい) 23 MOV12 (LG_PAGE #0xc→#0xe)
18 ADDSUBx4096_K8    10 ADDSUBx4096_K4_sp  7 LSR  7 K1024_sp  6 K19  6 K14  ...
```

lg12→lg14 で何が起きるかの**実測 ground truth** (fedora:42 実ビルド diff, 抜粋):

```
ubfx r,r,#12,#18  => #14,#17      ×248   ← PAGE定数によるビットフィールド再構成
ldrb [r,#816]     => [r,#848]     ×118   ← tsd/arena の struct offset +32
add #0x10,lsl#12  => #0x40,lsl#12      ← ページ比一定値
mov #0x1000 => #0x4000  ×43 / and #0xfff => #0x3fff ×66 / lsr #12=>#14 ×10
mov #0xc => #0xe  ×10  ← LG_PAGE 自体を即値に埋め込む箇所
mov #0x24 => #0x2c     ← SC_NSIZES など SC_* 定数も連動
add #0x13,lsl#12 => #0x12,lsl#12 ← 減る定数もある。×4 則だけでは錯
cmp/cmn #0x5,lsl#12 => #0x10... / b,b.eq,bl のターゲットシフトは無変換でよい
```

**設計帰結**: 命令テンプレート×4 一本槍は不可 (`#0x13→#0x12` 型が存在するため)。
パッチは **(A) 文脈審査済みサイトテンプレート** と **(B) 実測 census の literal value
写像** のマージで構成し、両者が競合したら census を正とする。census に無い値は
MANUAL 報告して人間が source で裏を取る。最終検証は **Pi (16K カーネル) で
パッチ済み exe を直接 boot** — jemalloc の初期化は arena/tcache/sysarena が
全定数を貫く整合性を要求するので、1 つでも漏れがあれば即 abort/crash、
通れば極めて強い正确性の証拠になる。

### AArch64 テンプレート対応表 (as で接地済み ground truth)

```
and #-4096→#-16384: 9274cd6a→9272c56a   orr #-4096→: b274cd6a→b272c56a
and #4095→#16383:  92402d6a→9240356a   lsr x: d34cfd6a→d34efd6a  lsl x: d374cd6a→d372c56a
asr x: 934cfd6a→934efd6a  (w: 530c7d6a→530e7d6a / 53144d6a→5312456a / 130c7d6a→130e7d6a)
mov #0x1000→#0x4000: d282000a→d288000a  mov #0xfff→#0x3fff: d281ffea→d287ffea (w: 52系同型)
add/sub/cmp #0x1,lsl#12→#0x4: +0xC00 (9140056a→9140116a, d1/b1/f1 同)
LG_PAGE: mov #0xc→#0xe (52800189→528001c9)  cmn #0xfff→#0x3fff: 表現不可→放置で正しい
```

## CI パイプライン

| workflow | 内容 |
|---|---|
| `jem16k-analysis.yml` | extract (公式イメージ→exe 抽出+sha256+指紋) / toolchains ×16 (debian12,u22,u24,fedora42 × CFLAGS{-ffunction-sections…} × --enable-prof、crate tarball を formula と同一の configure フラグで lg12+lg14 二重ビルド、指紋・patches.json 生成) / analyze (一致率 + census、結果を **check-run annotation** で公開 = 匿名で読める) |
| `build-16k.yml` | 夜間ビルダー (ureo/appflowy_worker_16k): detect (upstream 最新タグ検出) → build ×2arch (registry cache) → manifest → **fidelity** (公式 exe と ours を rustfilt で demangle 正規化した appflowy 系関数集合で比較。intersection=0 のみ fail、ドリフト内容はジョブ要約にレポート。build skip 時にも実行) → **smoke** (amd64 を postgres/redis/minio 付きで起動、/metrics 200 + 25s survival + panic 検査。失敗時は deps/worker ログをジョブ要約へ自動退避。ビルド有無に関係なく毎晩実行) |

実績: **run#6 (40193711212) = 16/16 ツールチェーン成功**・extract/analyze 成功、
結果注釈を匿名取得済み。過去 run の失敗原因(.arm ランナーに docker 無し→install、
slim イメージ git/ca-certificates 無し→apt、federation のみ→crate tarball 優先、
`| tee` の終了コードマスク→pipefail、ffs で .text 分裂→symtab sectional 指紋)
はすべて修正済み。

### 実行方法と結果の読み方

1. Actions → "jem16k GO/NO-GO analysis" → Run workflow (既定値で OK)
2. 完了後 run ページ最下部 **Annotations** の `SUMMARY part N` を読む
   (artifact zip と違い**ログイン不要**。`/check-runs/<analyze_id>/annotations`
   を curl するだけ)
3. 深掘りしたい toolchain は page 下部 Artifacts の `tc-<job-index>.zip` を
   DL (要ログイン)。中身: b12/ b14/ 完全ビルドツリー、fp12/fp14.txt、
   patches.json、t12/t14 テストバイナリ(16K ホストで直接実行可能)、control.txt

## scripts

- `jem16k.py` — ELF 読み・関数指紋 (`fp`)・lg12↔lg14 差分抽出 (`diff`)・
  指紋照合率 (`match`)。単一 `.text` も `-ffunction-sections` の `.text.<func>`
  分裂型も symtab で吸収。ページ由来即値以外をマスクした上で SHA1
- `patch_worker.py` — 公式 exe のページ定数サイトをテンプレート+census で
  書き換えて 16K 版を出力 (report-first、`--apply` で実書き込み)
- `wscan_worker.py` — 語レベル census 差分ストリーミング patcher
  (symtab スコープ + ページ派生 whitelist + t14 ground-truth 検証)。
  Plan B 実証用の実験ツールとして記録 (NO-GO 結論後も再現Hands本体)
- `ci_toolchain_build.sh` / `t16k.c` — コンテナ内 jemalloc 二重ビルド +
  ページ整合性テストプログラム / `post_comment.py` — 結果の commit comment 試行
  (GITHUB_TOKEN では 403: "Resource not accessible by integration"。annotation
  側で代替済み)

## Plan B (公式 exe のバイナリパッチ) 最終評価: **NO-GO (実証)**

源码変更不要で公式 `appflowyinc/appflowy_worker` exe 自体を 16K 化する
バイナリパッチ経路を最後まで実装した結果、** jemalloc のビルド同一性が
確保できないため sound に完了不可能であることが実証された **。

### 実施した最良の実装
- `wscan_worker.py`: ストリーミング語_level 差分 census パッチャ。
  symtab 実サイズで _rjem 関数スコープを厳密化 (dynsym では Rust 側が
  偽 _rjem 化して 91,928 サイトが毒になった)。
- ページ派生 whitelist (`pair_ok`): movz #0x1000→#0x4000 / ubfm shift
  12→14 のみ許可 → 125 語で EACG 手前まで正常。
- **ground truth 検証**: 16K 版テストビルド t14 の語集合に変換後語が
  実在するサイトのみ適用 (621 語: ldr 132 / add 96 / and 89 / ubfm 78 …)。
  `add x23,#0x8,lsl #12`→`#0x20,lsl #12` 等で **EACG クラッシュは解消**
  したが、次機能 (bin_info_boot) で別失敗が連鎖。

### NO-GO の決定的根拠
- worker の jemalloc 関数内語集合 ∩ t12 = **43.7%** (∩t14 = 40.8%) —
  tc 二重ビルドと公式同梱 jemalloc は **同一ビルドですらない**
  (関数指紋一致率 1.2–1.9% と整合)。
- tc で実測した struct フィールド offset 移動は **任意値**
  (例: 25648→23296 = −2352)。Δ=12288 の倍数という算術仮定は棄却され、
  43.7% の語共有しか無い別ビルドへ offset を移植すると
  残 56% の-site が不整合になる (crash matrix で実証:
  pagefunc 単体=rust_oom / 全 decision=EACG ldar / 厳密 125=EACG /
  W14 検証 621=bin_info_boot)。
- 結論: 公式 exe のみの情報では 16K 再コンパイルと語位に一致する
  変換表を生成不能。**16K 化はソース (jemalloc lg=14) ビルド一択**。

### 副産物として確定した事実 (修正済み理解)
- PIE bias (非 ASLR gdb) = **0x555555550000**、vaddr→file offset =
  −0x100000 (exec PT_LOAD off=0x119fe80/vaddr=0x11afe80)。
- `ldr x1,[x1,#25648]` (foff 0x48df7cc, EACG 内) は **実在** する
  (以前の「worker 側に無い」は bias 1 nibble 誤差的的中)。
- objdump `--start-address` はこの exe で空出力になる運用上の癖あり →
  フルダンプ (`disas_full.txt`) か gdb ライブで照合すること。

## 状態 / 残作業

- [x] jemalloc provenance 確定 / analysis 全緑 / 16K 動作実証 / GO 判定
- [x] patch_worker.py / wscan_worker.py 実装 → **Plan B は NO-GO 実証 (上節)**
- [x] 16K 実運用 = ソースビルド `ureo/appflowy_worker_16k` (稼働確認済み Up)。build-16k.yml はソースビルド系として維持 (バイナリパッチ job は追加しない)
- [ ] dc/appflowy/compose.yml worker へ healthcheck (`/metrics` 200 ポール) 適用
- [x] 空 DB に対する worker 起動スモーク (Pi 実測: 自前 migration で /metrics 200 + 生存確認 2026-10-26)
