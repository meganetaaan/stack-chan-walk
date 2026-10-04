# 出典と適用範囲

確認日: 2026-09-21。RT記事は実例として要約し、記事の数値/回路構成を
stack-chan-walkの仕様として転用しない。記事全文/画像の転載は行わない。

| ID | 一次情報 | 抽出した教訓 |
| --- | --- | --- |
| RT-31 | [ししかわ Part.31: 回路ブロック図](https://rt-net.jp/mobility/archives/13448/) | 機能ブロックとブロック間の接続を先に整理する。 |
| RT-37 | [ししかわ Part.37: M5StackとSTM32周辺回路](https://rt-net.jp/mobility/archives/15317/) | 接続相手のピン配置/向きと周辺回路を確認する。 |
| RT-39 | [ししかわ Part.39: フットプリント割当〜基板外形](https://rt-net.jp/mobility/archives/15534/) | 調達可能な型番から外形/ピンを照合する。外形Rや穴を機構と整合させ、面付けも確認する。記事固有のR2.5は本基板の規定値ではない。 |
| RT-40 | [ししかわ Part.40: 部品配置と配線](https://rt-net.jp/mobility/archives/15616/) | 機構拘束を先に配置する。電源往復の電流経路、推奨レイアウト、実装性、製造出力を確認する。 |
| RT-21 | [はしもと Part21: 配線を意識した部品配置](https://rt-net.jp/mobility/archives/23730/) | 製造ルールを先に設定し、パスコン/発振部品を含め配置段階でレビューする。 |
| RT-22 | [はしもと Part22: 配線のやり方と注意点](https://rt-net.jp/mobility/archives/23732/) | GNDの細い接続や帰路を見直す。電流と配線幅の目安を製造条件から切り離さない。 |
| RT-34 | [しゅう part34: 基板修正](https://rt-net.jp/mobility/archives/21695/) | 部品値だけでなく電流定格を照合し、電源部品も放熱レビューする。外形角を丸めた例があるが、R5は本基板の規定値ではない。 |

## 追加要求とプロジェクトへの具体化

`USER`: 依頼者が2026-09-21の依頼で指定した「基板の外径にRを付ける」
「シルクとパッドを干渉させない」「ロゴとリビジョン番号を付与する」を必須項目化した。
ロゴ/版番号の必須化などを、RT記事の記述だと偽って帰属させない。

`PROJECT`: このリポジトリの設計/受入条件と、そこから具体化したレビュー要求。
適用時には対象コミットの資料とIssueを再確認する。

- [#29 回路図・BOM](https://github.com/meganetaaan/stack-chan-walk/issues/29)
- [#30 配線・ハーネス解析](https://github.com/meganetaaan/stack-chan-walk/issues/30)
- [#31 機構統合・製造出力](https://github.com/meganetaaan/stack-chan-walk/issues/31)
- [#6 電源設計](https://github.com/meganetaaan/stack-chan-walk/issues/6)

## 自動チェックとSkill形式

`KICAD`: [KiCad 9 CLI](https://docs.kicad.org/9.0/en/cli/cli.html)、
[KiCad 10 CLI](https://docs.kicad.org/10.0/en/cli/cli.html)、
[PCB Editorの設計ルールとReadability DRC](https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html)。
CLIのERC/DRC、警告/除外の出力、違反時終了コード、シルク-マスク開口/外形の検査を
利用する。KiCad 10の`--refill-zones`は`--save-board`なしではPCBを保存しない。
9のCLI仕様には同オプションがないため、実装は能力を確認し、非対応なら事前再充填を
明示的に確認させる。シルク同士の図形交差をすべて検出できるわけではない。

ローカルSkill配置/メタデータは[OpenAIのSkillドキュメント](https://developers.openai.com/codex/skills/)
に従い、`.agents/skills/robot-board-review/SKILL.md`へ置く。
