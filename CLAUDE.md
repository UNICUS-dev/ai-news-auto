# 作業ルール（ai-news-auto）

## デプロイ同期ルール（必須）
このリポジトリを修正するときは、**必ず GitHub とローカル（Mac の `~/ai-news-auto`）の両方を更新する**こと。
片方だけ更新して終わりにしない。

理由: 実際に記事を生成・投稿するのは Mac 上のローカルコード（launchd が実行）。
GitHub を更新してもローカルへ自動同期されないため、GitHub だけ直すと本番挙動は変わらない。

### 手順
1. 変更は GitHub（master）とローカル `~/ai-news-auto` の両方へ反映する。
2. ローカルに未コミットの変更（例: `generate_slug` によるスラッグ生成）が存在し得るので、
   単純上書きせず、差分を確認して**マージ**する。上書き前に必ずバックアップ（`*.bak_日時`）を取る。
3. 反映後、両者の `config/config.yaml` と `src/post_dedup_value_add.py` が一致していることを確認する。
4. `python3 -m py_compile src/post_dedup_value_add.py` で構文チェックする。

## 本番実行に関する注意
- 実行スクリプト: `run_once_v3.sh`（venv で `src/post_dedup_value_add.py` を実行、1本投稿）。
- スケジュール: `config/config.yaml` の `schedule.times_jst`（現在 07:00 JST の朝1本）。
- 投稿には `.env` の `ANTHROPIC_API_KEY` と `WP_APP_PASSWORD` が必要（秘密情報）。

## これまでの主な改修
- ソース多様性: `domain_cooldown_days: 7`、`source_diversity`（30日で同一ドメイン最大2本＋スコア逓減）、`state/domain_history.json` で履歴管理。
- 記事の深掘り: howto_introduction / howto_creation / news_summary のプロンプトで、各工程・各事例を独立H3で省略せず解説。本文目標 3,000字。
- ローカル独自機能: `generate_slug`（日本語タイトル→英語SEOスラッグ）を維持すること。
