#!/usr/bin/env bash
# GitHub Actions コンテナ内で --with-lg-page=12 と 14 の jemalloc を両ビルドし、
# 命令差分 JSON / 関数指紋 / 16K 起動対照テストを ctx/ に残す。
# 前提: /ws にリポジトリが bind mount され、cwd は /ws/ctx。
set -euo pipefail
# 取得元: tikv-jemallocator 0.6.x が vendoring している jemalloc。
# crate 版の "+" サフィックスが公式バイナリ埋込み文字列
# (5.3.0-1-ge13ca993e8ccb9ba9847cc330696e02839f328f7) と完全一致する。
CRATE_VER="${CRATE_VER:-0.6.1+5.3.0-1-ge13ca993e8ccb9ba9847cc330696e02839f328f7}"
JEMALLOC_REF="${JEMALLOC_REF:-0.6.1}"   # tikv/jemallocator のタグ (フォールバック)
EXTRA_CFLAGS="${EXTRA_CFLAGS:-}"
PROF_FLAG="${PROF_FLAG:---enable-prof}"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates curl autoconf automake libtool build-essential git \
    python3 pkg-config file >/dev/null
  # debian/ubuntu 公式イメージは ca-certificates 同梱でないので curl/git が
  # https で全滅する (run#5 で CAfile: none を確認)。apt 経由で入れたら有効化。
  update-ca-certificates >/dev/null 2>&1 || true
else
  dnf install -y -q autoconf automake libtool git gcc gcc-c++ make \
    python3 pkgconf diffutils patch >/dev/null
fi

# --- jemalloc 源の取得 -----------------------------------------------------
# 注意: e13ca993... は jemalloc/jemalloc 本体には存在しない (tikv fork のみ)。
# 1) crates.io tarball (公式が実際にビルドした実源そのもの)
# 2) 失敗時: github.com/tikv/jemallocator タグ clone (vendored + 同梱 configure)
ok=0
if command -v curl >/dev/null 2>&1    && curl -fsSL --retry 3 -o crate.crate \
      "https://static.crates.io/crates/tikv-jemalloc-sys/tikv-jemalloc-sys-${CRATE_VER}.crate"; then
  tar xf crate.crate && rm -f crate.crate
  mv "tikv-jemalloc-sys-${CRATE_VER}" crate
  SRC=crate/jemalloc; CONF=crate/configure/configure
  JE_VERSION="${CRATE_VER#*+}"
  echo "source=crate tikv-jemalloc-sys-${CRATE_VER}" > source.txt
  ok=1
else
  git clone -q --branch "$JEMALLOC_REF" --single-branch --depth 1 \
    https://github.com/tikv/jemallocator repo
  SRC=repo/jemalloc-sys/jemalloc; CONF=repo/jemalloc-sys/configure/configure
  JE_VERSION=$(sed -n 's/^version *= *"\([^"]*\)+\([^"]*\)".*/\2/p' \
                 repo/jemalloc-sys/Cargo.toml | head -1)
  echo "source=tikv/jemallocator tag $JEMALLOC_REF (crate 取得失敗のフォールバック)" > source.txt
fi
[ -d "$SRC" ] || { echo "想定外のソース構成"; ls -R . | head -60; exit 1; }
[ -n "$JE_VERSION" ] || JE_VERSION=unknown
gcc -v > gcc-version.txt 2>&1
{ echo "${TAG:-unknown}"; cat source.txt; echo "JE_VERSION=$JE_VERSION"; } > meta.txt

for LG in 12 14; do
  # build.rs と同じ手順: 源ツリーをビルド dir へコピーし、同梱 configure を
  # 上書きしてツリー内で実行する (in-tree build)。
  rm -rf "b$LG"
  cp -a "$SRC" "b$LG"
  [ -f "$CONF" ] && cp "$CONF" "b$LG/configure"
  if [ ! -f "b$LG/configure" ]; then
    ( cd "b$LG" && autoconf ) || { echo "configure が見つからず"; exit 1; }
  fi
  ( cd "b$LG" \
    && sh ./configure --with-version="$JE_VERSION" --disable-cxx --enable-doc=no \
         --enable-shared=no ${PROF_FLAG} --enable-stats \
         --with-jemalloc-prefix=_rjem_ --with-private-namespace=_rjem_ \
         --with-lg-page="$LG" CC=gcc CFLAGS="$EXTRA_CFLAGS" \
         > configure.log 2>&1 \
    && make -j"$(nproc)" > build.log 2>&1 )
  grep -q "define LG_PAGE $LG" \
    "b$LG/include/jemalloc/internal/jemalloc_internal_defs.h" \
    || { echo "LG_PAGE=$LG の設定に失敗"; tail -20 "b$LG/configure.log"; exit 1; }
  # 指紋: cmd_fp は単一 .text 型 (exe/通常 .o) も .text.<func> 分裂型
  # (-ffunction-sections, run#5 fedora ffs の '関数 0 件') も symtab で吸収。
  # ビルドツリー b$LG/ を直接舐める。all$LG.o 合成はおまけ (多重定義で
  # ld -r が失敗することがあるので必須にしない)。
  mkdir -p ctx/fp/"$LG"
  python3 /ws/scripts/jem16k.py fp "b$LG" --filter _rjem > "fp$LG.txt" \
    || { echo "指紋化失敗"; exit 1; }
  cp "fp$LG.txt" ctx/fp/"$LG"/
  ls "b$LG"/src/*.o 2>/dev/null | grep -v '\.pic\.o$' \
    | xargs -r ld -r -o "all$LG.o" 2>/dev/null || :
  gcc -O2 -o "t$LG" /ws/scripts/t16k.c "b$LG/lib/libjemalloc.a" \
    -lpthread -lm -ldl
done

# ホスト実行対照: compile/link はページサイズ非依存だが実行時 init は整合必須。
# lg ページ == ホストページ のビルドのみ OK が期待値 (不一致側は abort 正常)。
HP=$(getconf PAGE_SIZE)
{
  echo "host PAGE_SIZE=$HP"
  for LG in 12 14; do
    SZ=$((1 << LG))
    set +e; "./t$LG" > "t$LG.out" 2>&1; RC=$?; set -e
    if [ "$SZ" = "$HP" ]; then EXP='rc=0 + OK'; else EXP='!=0 + abort 系'; fi
    echo "lg$LG (page=$SZ) rc=$RC expect: $EXP | out: $(head -c 120 "t$LG.out" | tr '\n' ' ')"
  done
} > control.txt
cat control.txt

if [ -s all12.o ] && [ -s all14.o ]; then
  python3 /ws/scripts/jem16k.py diff all12.o all14.o --filter _rjem > patches.json
else
  python3 /ws/scripts/jem16k.py diff b12 b14 --filter _rjem > patches.json
fi
echo "done: $(ls -1 *.txt *.json | tr '\n' ' ')"
