# appflowy-worker-builder

16KB ページサイズカーネル (Raspberry Pi OS 等の aarch64 16K 版) で動作する
**AppFlowy worker** イメージを自動ビルド・公開するリポジトリ。

公開先: **[ureo/appflowy_worker_16k](https://hub.docker.com/r/ureo/appflowy_worker_16k)**

## 背景

公式 `appflowyinc/appflowy_worker` の 0.14 系以降は jemalloc (4KB ページ前提ビルド) が
静的リンクされており、16KB ページのカーネルでは起動時にクラッシュする
(`Unsupported system page size`, exit 133)。

AppFlowy-Cloud の OSS ソースには jemalloc が存在しないため、
OSS ソース (AppFlowy-IO/AppFlowy-Cloud) から直接ビルドすれば 16KB ページ環境で動作する。

## 仕組み

GitHub Actions (`cron` 毎晩 JST 0:00):

1. Docker Hub を検索し `appflowyinc/appflowy_worker` の最新セマンティックバージョンを検出
2. `ureo/appflowy_worker_16k` に同バージョンが無ければ OSS ソースからビルド
3. upstream と同じタグ構成でプッシュ:
   - `{VER}-amd64` / `{VER}-arm64v8` (ネイティブランナーでビルド)
   - マニフェストリスト `{VER}` / `latest`
4. ビルド後にバイナリを検査 (jemalloc 文字列ヒット数 0 を確認、非 0 なら失敗扱い)

手動実行: Actions タブ → Build and push 16K worker → Run workflow
(version / force 指定可)

## 使い方 (self-host, 16KB ページ環境)

```yaml
 services:
   worker:
-    image: appflowyinc/appflowy_worker:latest
+    image: ureo/appflowy_worker_16k:latest
     platform: linux/arm64
```

```bash
docker compose pull worker && docker compose up -d worker
```

## ビルドキャッシュ

`type=registry` (Docker Hub) を採用。ビルド時に中間段込み (`mode=max`) の
BuildKit キャッシュを OCI artifact として `ureo/appflowy_worker_16k:buildcache-{amd64,arm64v8}`
へ書き出し、次回は `cache-from` から復元する。Docker Hub 上のタグとして見えるため、
`-`(ハイフン) 付き納品タグと区別しやすい `buildcache-` 接頭辞を付けている。

- 効果: ソース凍結のため次回ビルドは cargo chef cook 層まで全ヒット (30分 → 数分)
- Docker Hub 選択理由: 納品物と同じレジストリで認証を統一 (DOCKERHUB_TOKEN のみ)、
  公開リポジトリなのでストレージ実質無制限。GitHub Actions cache (type=gha) は
  7日未アクセスで失効するため、ビルドが滅多に走らない本運用には不向き

## 注意点

- ソースはアーカイブ済み OSS リポジトリの `main` のため、機能的には OSS 最終段階相当。
  バージョンタグは upstream 追従で付与されるが、バイナリ自体は同一ソースから生成される
  (upstream 新機能を継承するわけではない)。
- worker はインポート/公開/メール等のバックグラウンドジョブのみ担当。
  同期・API は cloud が担当するため中核機能への影響はない。
