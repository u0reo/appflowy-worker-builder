#!/usr/bin/env bash
# GitHub Actions コンテナ内で --with-lg-page=12 と 14 の jemalloc を両ビルドし、
# 命令差分 JSON / 関数指紋 / 16K 起動対照テストを ctx/ に残す。
# 前提: /ws にリポジトリが bind mount され、cwd は /ws/ctx。
set -euo pipefail
: "${JEM_COMMIT:?JEM_COMMIT を設定}"
EXTRA_CFLAGS="${EXTRA_CFLAGS:-}"
PROF_FLAG="${PROF_FLAG:---enable-prof}"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    autoconf automake libtool build-essential python3 pkg-config file >/dev/null
else
  dnf install -y -q autoconf automake libtool gcc gcc-c++ make python3 \
    pkgconf diffutils >/dev/null
fi

git clone -q https://github.com/jemalloc/jemalloc jem
git -C jem -c advice.detachedHead=false checkout "$JEM_COMMIT"
( cd jem && autoconf )   # configure スクリプト生成 (リポジトリには同梱されていない)
gcc -v > gcc-version.txt 2>&1
printf '%s\n%s\n' "${TAG:-unknown}" "$(cat jem/VERSION)" > meta.txt

for LG in 12 14; do
  mkdir -p "b$LG"
  ( cd "b$LG" \
    && ../jem/configure --with-jemalloc-prefix=_rjem_ \
         --with-private-namespace=_rjem_ --disable-cxx --enable-doc=no \
         --enable-shared=no ${PROF_FLAG} --enable-stats \
         --with-lg-page="$LG" CC=gcc CFLAGS="$EXTRA_CFLAGS" \
         > configure.log 2>&1 \
    && make -j"$(nproc)" > build.log 2>&1 )
  grep -q "define LG_PAGE $LG" \
    "b$LG/include/jemalloc/internal/jemalloc_internal_defs.h" \
    || { echo "LG_PAGE=$LG の設定に失敗"; tail -20 "b$LG/configure.log"; exit 1; }
  python3 /ws/scripts/jem16k.py fp "b$LG" --filter _rjem > "fp$LG.txt"
  gcc -O2 -o "t$LG" /ws/scripts/t16k.c "b$LG/lib/libjemalloc.a" \
    -lpthread -lm -ldl
done

# 16K ホスト対照: lg12 は abort、lg14 は OK 出力が正しい結果
set +e
./t12 > t12.out 2>&1; rc12=$?
./t14 > t14.out 2>&1; rc14=$?
set -e
{
  echo "lg12 rc=$rc12 (16K では !=0 + 'Unsupported system page size' 想定)"
  echo "lg14 rc=$rc14 (=0 + OK 想定)"
  grep -o 'Unsupported system page size' t12.out | head -1 || true
  echo "lg14 stdout: $(cat t14.out)"
} > control.txt
cat control.txt

python3 /ws/scripts/jem16k.py diff b12 b14 --filter _rjem > patches.json
echo "done: $(ls -1 *.txt *.json)"
