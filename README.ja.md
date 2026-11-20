# isu-bench

[English](README.md) | 日本語

isu-benchは、コーディングエージェントに、初めて見るWebサービスとそれが動く3台のサーバーを渡します。ベンチマークは11回まで。時間制限もヒントもなく、質問できる相手もいません。11回目の結果がそのままスコアになります。

課題の詳しい説明、グラフ、すべての数値は **[isu-bench.trasta.dev](https://isu-bench.trasta.dev/)** にあります。

![isu-bench v1.1 の結果](docs/isu-bench-v1.1.ja.png)

## 結果

isu-bench v1.1の結果です。各構成の試行は1回だけなので、小さな差に意味はありません。スコアは、11回目のベンチマークの点数を理論上の上限の見積もり（1,060,000点）で割った割合です。

| モデル | エージェント | effort | スコア | 作業時間 | API換算の費用 |
|---|---|---|---:|---:|---:|
| Claude Fable 5.2 | Claude Code 2.1.321 | high | **58.4%** | 4時間31分 | $76.55 |
| Gemini 4 Neon | Antigravity CLI 1.5.12 | high | **54.7%** | 2時間4分 | $18.98 |
| Gemini 4 Carbon | Antigravity CLI 1.5.5 | medium | **51.9%** | 3時間9分 | $39.27 |
| Claude Opus 5.5 | Claude Code 2.1.321 | high | **43.2%** | 2時間13分 | $22.78 |
| GPT-6.2 Astra | Codex 0.169.0 | xhigh | **41.8%** | 35分 | $9.70 |
| Gemini 4 Hydrogen | Antigravity CLI 1.5.12 | high | **17.8%** | 1時間21分 | $10.52 |
| GPT-6.1 Sol | Codex 0.166.0 | xhigh | **15.2%** | 46分 | $2.38 |
| GPT-6.2 Sol | Codex 0.169.0 | xhigh | **14.1%** | 48分 | $4.14 |
| GPT-6 Astra | Codex 0.166.0 | xhigh | **9.7%** | 46分 | $13.72 |
| DeepSeek V4.1 Flash | DeepSeek Harness 0.2.1-rc.5 | max | **0.8%** | 48分 | $0.42 |

- 初期状態では約1,744点（0.2%）で、実行ごとに5%ほどばらつきます。
- API換算の費用は、各試行のトークン数に2026年11月時点の各社の定価を掛けた値です（[isubench/prices.py](isubench/prices.py)）。DeepSeekはピーク時の料金、Gemini 4 Carbonは導入価格（12月1日から2倍）で計算しています。

## 仕組み

- エージェントに渡すのは、仕様書、Goで書かれた遅い参照実装、3台のサーバーのroot権限です。仕様どおりに動き続ける限り、何を変えても構いません。
- `bench` でベンチマークを実行します。FAILも含めて最大11回です。11回目の結果が最終スコアになり、FAILなら0点です。
- その後サーバーを再起動します。サービスが復帰しない、またはデータが残っていなければ0点です。
- 時間制限はなく、Webは使えません。

課題の形式は、2011年から続くWebアプリケーション高速化コンテスト[ISUCON](https://isucon.net/)にならっています。問題の「ISUMAIL」はisu-bench用に新しく作ったもので、モデルが既存の解法を見ていないよう、ソースコードは公開していません。配布はマシンイメージのみで、このリポジトリに含める試行結果も `result.json` のみにしています。

ルール全文は [REQUIREMENTS.md](REQUIREMENTS.md)、プロンプトの原文は [prompt.md](prompt.md) にあります。

## 何を測っているか

| | テストで採点するベンチマーク | isu-bench |
|---|---|---|
| 作業内容 | リポジトリのコードを変更する | 稼働中のシステム全体（コード、データベース、3台のサーバー）を最適化する |
| 終了条件 | テストが通れば終わり | 終わりはない。改善した分だけスコアが伸びる |
| フィードバック | テストは何度でも実行できる | ベンチマークは11回まで。最後の1回がFAILなら0点 |
| 問題の公開状況 | 多くは公開されているコード | 非公開の問題。マシンイメージのみで配布 |

高いスコアを出すには、次の力が同時に求められます。

- 初めて見る仕様書とコードを読み解く
- 計測してボトルネックを見つける
- 大きな書き換えを、動作を壊さずにやり切る
- デプロイ、再起動後の復帰、データの永続化といったサーバー運用
- 限られた回数の中で、いつ計測し、どこでリスクを取るのをやめるか判断する

## 実行方法

動作要件: Dockerが動作するLinux、`ap-northeast-1` の認証情報を設定したAWS CLI v2（1試行でオンデマンドのCインスタンス13 vCPU）、Python 3.10以上、git、ssh。費用は1試行につきEC2が1時間あたり約$0.64で、これにモデルの利用料が加わります。

```sh
export ANTHROPIC_API_KEY=...   # または CLAUDE_CODE_OAUTH_TOKEN / OPENAI_API_KEY / DEEPSEEK_API_KEY / OLLAMA_API_KEY / GEMINI_API_KEY
python3 -m isubench run --problem isumail --agent claude-code --model claude-opus-5-5 --effort high
python3 -m isubench table        # results/ にあるすべての結果
python3 -m isubench teardown <run_id>
```

1回の試行ごとに専用のVPCとインスタンスを作成し、終了時に自動で削除します。ハーネスが異常終了しても、インスタンスは48時間後に自動終了します。`teardown` で即座に削除することもできます。

| エージェント | `--agent` | 補足 |
|---|---|---|
| Claude Code | `claude-code` | |
| Codex | `codex` | isu-bench専用のChatGPTログインも使えます: `docker run --rm -it -v ~/.config/isu-bench/codex:/home/agent/.codex isu-bench-agent:v1 codex login --device-auth` |
| Antigravity CLI | `agy` | Gemini APIのモデルを名前で指定します（`--model gemini-4-carbon`）。`--effort` は `low`・`medium`・`high`。Web検索はモデルAPI側で実行されてプロキシを通らないため、フックで無効化しています |
| DeepSeek Harness | `dsh` | `OLLAMA_API_KEY` を設定するとOllama Cloudで動きます。ベンチマーク1回に約70秒かかるため、コマンドのタイムアウトを60秒から10分に延ばしています |

- `agy` では、isu-bench専用のGoogleログインも使えます。`GEMINI_API_KEY` が未設定のときに使われます。ログインには `docker run --rm -it -v ~/.config/isu-bench/agy:/home/agent/.gemini/antigravity-cli isu-bench-agent:v1 agy` を実行してGoogle OAuthを選び、ログインページに表示されるコードを貼り付けます。初回画面では、Googleによる操作データ収集への同意にチェックが入っているので外します。ログイン後は、`agy models` でそのアカウント向けに一覧表示されるモデルを指定できます（例: `--model gemini-4-neon --effort high`）。

## 他の問題と以前のバージョン

- ISUCON14とISUCON13もこのハーネスで動きます（`--problem isucon14`、`--problem isucon13`）。公開されている解法がモデルの学習時点より前に存在するため、採点には使っていません。v1の結果は `results/` にあります。
- v1はベンチマークが15回で、ISUMAILのベンチマーカーも古いバージョンでした。v1.1のスコアとは比べられません。

## クレジット

- ISUMAILの設計で大いに参考にした問題: [ISUCON2026](https://isucon.net/)
- 問題、参照実装、ベンチマーカー: [isucon/isucon14](https://github.com/isucon/isucon14)
- AMI: [matsuu/aws-isucon](https://github.com/matsuu/aws-isucon)
- TTLの手法: [kyosu-1/isuenv](https://github.com/kyosu-1/isuenv)

## 商標

「ISUCON」は、さくらインターネット株式会社の商標または登録商標です。（[isucon.net](https://isucon.net)）

isu-benchは独立した非公式のプロジェクトです。ISUCONの運営やさくらインターネット株式会社が主催、後援、公認するものではありません。

## ライセンス

[MIT](LICENSE)
