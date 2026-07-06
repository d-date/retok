# return-of-token (retok)

[English](README.md) | **日本語**

Claude Code の利用ログ（トランスクリプト）を解析し、**Token対効果**を測定・分析して改善アドバイスを表示するCLIツール。

Python 3 標準ライブラリのみで動作します（依存パッケージなし）。

## 使い方

```sh
./retok                    # 直近30日のレポート
./retok --days 7           # 期間を変更
./retok --project myapp    # プロジェクト名で絞り込み（部分一致）
./retok --lang ja          # レポート言語（デフォルト: $LANG）
./retok --json             # JSON出力（他ツール連携用）
./retok --top 20           # ランキング表の行数
./retok --dirs ~/somewhere/projects # スキャン対象を指定
```

デフォルトで `~/.claude/projects` を走査し、環境変数 `CLAUDE_CONFIG_DIR` が設定されていれば `$CLAUDE_CONFIG_DIR/projects` も対象に含めます。トランスクリプトを別の場所に置いている場合（複数プロファイルや独自の設定ディレクトリ等）は `--dirs` ですべてのルートを指定してください。同一メッセージIDはグローバルに重複排除されるため、ルートが重複していても二重計上されません。

## 何を測るか

| 指標 | 意味 |
|---|---|
| 推定コスト | モデル別公表価格（下表）から算出したUSD概算 |
| キャッシュヒット率 | `cache_read / (input + cache_read + cache_write)`。高いほど安い |
| プロンプトあたりコスト | 1回の人間の指示に対して消費したコスト |
| サブエージェント分コスト | Task/Explore等のサブエージェント（`<session>/subagents/agent-*.jsonl`）の消費分 |
| maxCtx | セッション中の最大コンテキストサイズ。大きいほど毎リクエストが高くつく |

## アドバイスの検出ルール

- **キャッシュTTL失効** — TTL（書込バケットから1h/5mを判定）を超えるギャップの直後に、コンテキストの過半を占める大きなキャッシュ書込が発生 → 放置セッション再開時の全再キャッシュ。`/compact` や `/clear` を推奨
- **巨大コンテキスト** — 120k tokens 超のセッションを列挙。タスク間 `/clear` の徹底を推奨
- **探索の委譲不足** — メインスレッドの Read/Grep/Glob 比率が高くサブエージェント利用が少ない場合
- **再試行ループ** — 同一Bashコマンドをセッション内で5回以上実行
- **中断多発** — `[Request interrupted by user]` がプロンプト数の12%超 → 指示の明確化・Plan Mode活用を推奨
- **高価格モデルでの小粒セッション** — 一問一答レベルの用途でOpus/Fableを使用

## コスト計算の前提

価格（USD / MTok、2026-06時点）:

| モデル | input | output |
|---|---|---|
| Fable 5 / Mythos 5 | $10 | $50 |
| Opus 4.x | $5 | $25 |
| Sonnet 4.x / 5 | $3 | $15 |
| Haiku 4.5 | $1 | $5 |

- キャッシュ読取 = input単価の **0.1×**
- キャッシュ書込 = input単価の **1.25×**（5分TTL）/ **2×**（1時間TTL）— usage の `cache_creation` バケットで判別
- 1つのAPI応答が複数エントリに分割記録されるため、`message.id` で重複排除して集計

> 注意: あくまで公表価格に基づく概算です。サブスクリプション（Max等）利用時の実支払額ではなく、「API換算でどれだけの計算資源を使ったか」の指標として読んでください。

## 対応言語

レポート言語は `--lang` → `RETOK_LANG` → `LC_ALL` / `LC_MESSAGES` / `LANG` の順で決まり、該当がなければ英語になります。

対応: `en`（組み込み）, `ja`, `zh-CN`, `zh-TW`, `ko`, `es`, `fr`, `de`, `pt-BR`

### 言語の追加方法

1. `locales/ja.json` を `locales/<tag>.json` にコピー（BCP 47風のタグ。例: `it`, `pt-PT`）
2. 値を翻訳（`{placeholders}` はそのまま残す）
3. `./retok --lang <tag>` で確認してプルリクエストを送る

未翻訳のキーは自動的に英語へフォールバックするので、部分的な翻訳でも動きます。

## ライセンス

[MIT](LICENSE)
