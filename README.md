# appflowy-worker-builder — 公式 appflowy_worker の 16K ページ対応バイナリパッチ

Raspberry Pi OS (2025-05+ カーネル 6.12.x rpi = **16K ページ**) 環境で AppFlowy Cloud を**セルフホスト**すると、公式の `appflowyinc/appflowy_worker` が起動しない問題が発生します。本リポジトリは、**公式バイナリに対してパッチを適用して 16K ページ対応版を作る** (Plan B 案) ための調査・分析・ビルド CI 群です。

現状: **Plan B は不可能と判定済み（NO-GO）。現在は Plan A (ソースビルド) による自動提供に移行しています。**

## 症状と原因

```text
<jemalloc>: Unsupported system page size   ← worker コンテナ起動直後に落ちてこれを出す
```

- 公式 worker (static link) の jemalloc が `--with-lg-page=12` (PAGE=4096) でビルドされている
- jemalloc は `pages_boot()` で `システムページ > ビルド時ページ` を検出すると、意図的に abort (自己防衛) する。これは機能不全ではなく、仕様である
- aarch64 の rust-std 既定は `max-regular-page=4096` なので、Rust ビルドでは 16K 想定のビルドが原理的に作れない $\rightarrow$ **公式イメージを出さない限り、バイナリパッチによる対応も困難である** $\rightarrow$ 公式 exe への後付けパッチが唯一の現実的な解決策となる

## 証明済みの事実 (すべて実測)

| # | 事実 | 証明方法 |
|---|------|---------|
| 1 | 組込み jemalloc は e13ca993 (tikv/jemalloc fork, 5.3.0-1-g...) = tikv-jemalloc-sys 0.6.x の vendored ツリーとバイト同一 | バイナリ内バージョン文字列 + クレート tarball との diff |
| 2 | e13ca993 の jemalloc を `--with-lg-page=14` でビルド $\rightarrow$ **16K カーネルの Pi で `OK` rc=0** | CI ビルド成果物 t14 を Pi (6.12.47+rpt-rpi-2712, PAGE_SIZE=16384) で直接実行 |
| 3 | 同款 lg12 ビルド t12 は同じ Pi で公式と同一の `Unsupported system page size` rc=2 | 上と同時対照 |
| 4 | lg14 jemalloc は **4K ホストでも正常動作** (CI の armランナー=4K で t14 OK) | パッチ済み worker は 4K/16K 両対応になることを意味する |
| 5 | 公式 exe と CI 16 変種ビルドの関数解析結果に基づく一致率は **1.2〜1.9%** (関数名は 417/417 common) | jem16k.py match (run#6, 全 16 ツールチェーン) |
| 6 | lg12 $\leftrightarrow$ lg14 実測 diff のパターン頻度表 (census) はページ定数の写像として整合 | jem16k.py diff patches.json、run#6 tc-* |
| 7 | `cmn x,#0xfff` (公式 26 サイト) は lg14 ビルドでも**一切変化しない** = ページ非由来 | 実測 census に cmn キー 0 件 $\rightarrow$ **既存のコードを書き換えないことが正しいと確定** |

5 より「ビルド差分の 1:1 移植」は **NO-GO** (公式の codegen はどの CI gcc とも異なるため)。
判定 **GO** の根拠は 2, 6 および下記解析結果。

## パッチ対象の詳細解析結果 (公式 exe = ELF aarch64 PIE, jemalloc セクション `0x49b0000–0x4a00000`)

サイト台帳 `/tmp/review.txt`: 公式内ページ定数参照 **345 サイト**、内訳:

```text
71 ADDSUBx4096_K16   65 ADDSUBx4096_sp   49 ADDSUBx4096   29 MOVZ1000
26 ADDSUBfff (cmn #0xfff — 不変で正しい) 23 MOV12 (LG_PAGE #0xc $\rightarrow$ #0xe)
18 ADDSUBx4096_K8    10 ADDSUBx4096_K4_sp  7 LSR  7 K1024_sp  6 K19  6 K14  ...
```

lg12 $\rightarrow$ lg14 で何が起きるかの**実測 ground truth** (fedora:42 実ビルド diff, 抜粋):

```text
ubfx r,r,#12,#18  => #14,#17      ×248   $\leftarrow$ PAGE定数によるビットフィールド再構成
ldrb [r,#816]     => [r,#848]     ×118   $\leftarrow$ tsd/arena の struct offset +32
add #0x10,lsl#12  => #0x40,lsl#12      $\leftarrow$ ページ比一定値
mov #0x1000 => #0x4000  ×43 / and #0xfff => #0x3fff ×66 / lsr #12 $\rightarrow$ #14 ×10
mov #0xc => #0xe  ×10  $\leftarrow$ LG_PAGE 自体を即値に埋め込む箇所
mov #0x24 => #0x2c     $\leftarrow$ SC_NSIZES など SC_* 定数も連動
add #0x13,lsl#12 => #0x12,lsl#12 $\leftarrow$ 減る定数もある。×4 則だけでは不十分
cmp/cmn #0x5,lsl#12 => #0x10... / b,b.eq,bl のターゲットシフトは無変換でよい
```

**設計方針**: 命令テンプレートのみに依存したパッチ適用は不可能です (`#0x13 $\rightarrow$ #0x12` のような定数変化が存在するため)。パッチは、**(A) 文脈解析済みのテンプレート** と **(B) 実測した `census` によるリテラル値の写像** を組み合わせて構成します。両者が競合した場合は `census` の値を正とします。`census` に存在しない値が見つかった場合は、手動でソースコードの裏付け調査を行います。最終検証は **Pi (16K カーネル) 上でパッチ済み exe を直接起動** して行います。jemalloc の初期化は arena/tcache/sysarena が全ての定数を貫く整合性を要求するため、1 つでも漏れがあれば即 abort/crash し、成功すれば極めて高い正確性の証拠となります。

### AArch64 テンプレート対応表 (as で接地済み ground truth)

```text
and #-4096 $\rightarrow$ #-16384: 9274cd6a $\rightarrow$ 9272c56a   orr #-4096 $\rightarrow$: b274cd6a $\rightarrow$ b272c56a
and #4095 $\rightarrow$ #16383:  92402d6a $\rightarrow$ 9240356a   lsr x: d34cfd6a $\rightarrow$ d34efd6a  lsl x: d374cd6a $\rightarrow$ d372c56a
asr x: 934cfd6a $\rightarrow$ 934efd6a  (w: 530c7d6a $\rightarrow$ 530e7d6a / 53144d6a $\rightarrow$ 5312456a / 130c7d6a $\rightarrow$ 130e7d6a)
mov #0x1000 $\rightarrow$ #0x4000: d282000a $\rightarrow$ d288000a  mov #0xfff $\rightarrow$ #0x3fff: d281ffea $\rightarrow$ d287ffea (w: 52系同型)
add/sub/cmp #0x1,lsl#12 $\rightarrow$ #0x4: +0xC00 (9140056a $\rightarrow$ 9140116a, d1/b1/f1 同)
LG_PAGE: mov #0xc $\rightarrow$ #0xe (52800189 $\rightarrow$ 528001c9)  cmn #0xfff $\rightarrow$ #0x3fff: 表現不可 $\rightarrow$ 放置で正しい
```

## CI パイプライン

| workflow | 内容 |
|---|---|
| `jem16k-analysis.yml` | extract (公式イメージ $\rightarrow$ exe 抽出+sha256+指紋) / toolchains ×16 (debian12,u22,u24,fedora42 × CFLAGS{-ffunction-sections…} × --enable-prof、crate tarball を formula と同一の configure フラグで lg12+lg14 二重ビルド、指紋・patches.json 生成) / analyze (一致率 + census、結果を **check-run annotation** で公開 = ログインなしで閲覧可能) |
| `build-16k.yml` | 夜間ビルダー (ureo/appflowy_worker_16k): detect (upstream 最新タグ検出) $\rightarrow$ build ×2arch (registry cache) $\rightarrow$ manifest $\rightarrow$ **fidelity** (公式 exe と ours を rustfilt で demangle 正規化した appflowy 系関数集合で比較。intersection=0 のみ fail、ドリフト内容はジョブ要約にレポート。build skip 時にも実行) $\rightarrow$ **smoke** (amd64 を postgres/redis + (取れた場合)minio で起動。runner からの匿名 pull は minio だけ docker.io/quay.io とも unauthorized を返すため minio は optional (Pi 実測で S3 非到達でも起動 OK)。/metrics 200 + 25s survival + panic 検査。失敗時は deps/worker ログをジョブ要約へ自動退避。ビルド有無に関係なく毎晩実行) |

実績: **run#6 (40193711212) = 16/16 ツールチェーン成功**・extract/analyze 成功、結果注釈を匿名取得済み。過去 run の失敗原因 (.arm ランナーに docker 無し $\rightarrow$ install、slim イメージ git/ca-certificates 無し $\rightarrow$ apt、federation のみ $\rightarrow$ crate tarball 優先、`| tee` の終了コードマスク $\rightarrow$ pipefail、ffs で .text 分裂 $\rightarrow$ symtab sectional 指紋)はすべて修正済み。

### 実行方法と結果の読み方

1. Actions $\rightarrow$ "jem16k GO/NO-GO analysis" $\rightarrow$ Run workflow (既定値で OK)
2. 完了後 run ページ最下部 **Annotations** の `SUMMARY part N` を確認
   (artifact zip と違い**ログイン不要**。`/check-runs/<analyze_id>/annotations`
   を curl することによる取得が可能)
3. 深掘りしたい toolchain は page 下部 Artifacts の `tc-<job-index>.zip` を
   DL (要ログイン)。中身: b12/ b14/ 完全ビルドツリー、fp12/fp14.txt、
   patches.json、t12/t14 テストバイナリ(16K ホストで直接実行可能)、control.txt

## scripts

- `jem16k.py` — ELF 読み・関数解析 (`fp`)・lg12 $\leftrightarrow$ lg14 差分抽出 (`diff`)・
  解析結果の照合率 (`match`)。単一 `.text` も `-ffunction-sections` の `.text.<func>`
  分裂型も symtab で吸収。ページ由来即値以外をマスクした上で SHA1
- `patch_worker.py` — 公式 exe のページ定数サイトをテンプレート+census で
  書き換えて 16K 版を出力 (report-first、`--apply` で実書き込み)
- `wscan_worker.py` — 語レベル census 差分ストリーミング patcher
  (symtab スコープ + ページ派生 whitelist + t14 ground-truth 検証)。
  Plan B 実証用の実験ツールとして記録 (NO-GO 結論後も再現用として保持)
- `ci_toolchain_build.sh` / `t16k.c` — コンテナ内 jemalloc 二重ビルド +
  ページ整合性テストプログラム / `post_comment.py` — 結果の commit comment 試行
  (GITHUB_TOKEN では 403: "Resource not accessible by integration"。annotation
  側で代替済み)

## Plan B (公式 exe のバイナリパッチ) 最終評価: **NO-GO (実証)**

ソースコードを変更せずに、公式の `appflowyinc/appflowy_worker` バイナリ自体を 16K 化する
バイナリパッチ手法を検証した結果、** jemalloc のビルド同一性が
確保できないため、安全に完了させることは不可能であることが実証されました **。

### 実施した最良の実装
- `wscan_worker.py`: ストリーミング語_level 差分 census パッチャ。
  symtab 実サイズで _rjem 関数スコープを厳密化 (dynsym では Rust 側が
  偽 _rjem 化して 91,928 サイトが不整合を起こした)。
- ページ派生 whitelist (`pair_ok`): movz #0x1000 $\rightarrow$ #0x4000 / ubfm shift
  12 $\rightarrow$ 14 のみ許可 $\rightarrow$ 125 語で EACG 手前まで正常に動作。
- **ground truth 検証**: 16K 版テストビルド t14 の語集合に、変換後の語が
  実在するサイトのみ適用 (621 語: ldr 132 / add 96 / and 89 / ubfm 78 …)。
  `add x23,#0x8,lsl #12` $\rightarrow$ `#0x20,lsl #12` 等で **EACG クラッシュは解消**
  したが、次機能 (bin_info_boot) で別の失敗が連鎖。

### NO-GO の決定的根拠
- worker の jemalloc 関数内語集合 $\cap$ t12 = **43.7%** ($\cap$t14 = 40.8%) —
  tc 二重ビルドと公式同梱 jemalloc は **同一のビルドではありません**
  (関数解析結果の一致率は 1.2–1.9% と整合)。
- tc で実測した struct フィールド offset 移動は **任意値**
  (例: 25648 $\rightarrow$ 23296 = −2352)。$\Delta$ = 12288 の倍数という算術的仮定は棄却され、
  43.7% の語共有しか無い別ビルドへ offset を移植すると
  残る 56% のサイトが不整合になります (crash matrix で実証:
  pagefunc 単体=rust_oom / 全 decision=EACG ldar / 厳密 125=EACG /
  W14 検証 621=bin_info_boot)。
- 結論: 公式 exe から得られる情報のみでは、16K 再コンパイルと語位に一致する
  変換表を生成することは不可能です。**16K 化はソース (jemalloc lg=14) の
  ビルド一択となります**。

### 副産物として確定した事実 (修正済み理解)
- PIE bias (非 ASLR gdb) = **0x555555550000**、vaddr $\rightarrow$ file offset =
  −0x100000 (exec PT_LOAD off=0x119fe80/vaddr=0x11afe80)。
- `ldr x1,[x1,#25648]` (foff 0x48df7cc, EACG 内) は **実在** する
  (以前の「worker 側に無い」という推測は、bias による 1 nibble の誤差であった)。
- objdump `--start-address` はこの exe で空の結果を返すという運用上の癖があるため、
  フルダンプ (`disas_full.txt`) か gdb によるライブデバッグで照合すること。

## 状態 / 残作業

- [x] jemalloc provenance 確定 / analysis 全緑 / 16K 動作実証 / GO 判定
- [x] patch_worker.py / wscan_worker.py 実装 $\rightarrow$ **Plan B は NO-GO と判定 (上節参照)**
- [x] 16K 実運用 = ソースビルド `ureo/appflowy_worker_16k` (稼働確認済み)。build-16k.yml はソースビルド系として維持 (バイナリパッチ job は追加しない)
- [x] 空 DB に対する worker 起動スモーク (Pi 実測: 自前 migration で /metrics 200 + 生存確認 2026-10-26)
- [x] CI スモーク job 緑化 (2026-10-26 run 40217780446: detect/build×2/manifest/fidelity/smoke 全成功)。ハマりどころ：mcブロックの引用符エスケープミス (exit 2)、docker pull -q の結果が変数に混入して無効参照となる問題 (exit 125)、診断用標準出力がコマンド置換によって消失してしまう問題 ( $\rightarrow$ stderr と ERR trap、および GitHub アノテーションの活用により、認証なしのログ環境でも自己診断が可能に)
