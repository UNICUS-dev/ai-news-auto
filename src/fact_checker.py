# -*- coding: utf-8 -*-
"""
fact_checker.py
基本的なルールベースのファクトチェック機能
"""
import re
from typing import Dict, List, Set


def extract_numbers(text: str) -> Set[str]:
    """
    テキストから数値を抽出

    Returns:
        数値の文字列セット（"100", "3.5", "20%"など）
    """
    numbers = set()

    # 整数と小数
    numbers.update(re.findall(r'\d+(?:\.\d+)?', text))

    # パーセンテージ
    percentages = re.findall(r'\d+(?:\.\d+)?%', text)
    numbers.update(percentages)

    return numbers


def extract_dates(text: str) -> Set[str]:
    """
    テキストから日付を抽出

    Returns:
        日付の文字列セット
    """
    dates = set()

    # YYYY年MM月DD日形式
    dates.update(re.findall(r'\d{4}年\d{1,2}月\d{1,2}日', text))

    # YYYY/MM/DD, YYYY-MM-DD形式
    dates.update(re.findall(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}', text))

    # MM月DD日形式
    dates.update(re.findall(r'\d{1,2}月\d{1,2}日', text))

    # 英語の日付形式（November 6, 2025など）
    dates.update(re.findall(r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}', text, re.IGNORECASE))

    return dates


def extract_proper_nouns(text: str) -> Set[str]:
    """
    テキストから固有名詞を抽出（簡易版）

    主に英語の大文字で始まる単語、カタカナ語を抽出
    """
    nouns = set()

    # 除外する一般的な英単語
    common_words = {
        'The', 'A', 'An', 'This', 'That', 'These', 'Those',
        'You', 'Your', 'My', 'Our', 'Their', 'His', 'Her',
        'What', 'When', 'Where', 'Why', 'How', 'Who',
        'Can', 'Could', 'Will', 'Would', 'Should', 'May', 'Might',
        'To', 'From', 'With', 'Without', 'For', 'By', 'At', 'In', 'On',
        'But', 'And', 'Or', 'So', 'If', 'As',
        'New', 'Old', 'First', 'Last', 'Next', 'All', 'Some', 'Many',
        'It', 'Its', 'Is', 'Are', 'Was', 'Were', 'Be', 'Been',
    }

    # 英語の固有名詞（連続する大文字開始の単語）
    english_nouns = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
    for noun in english_nouns:
        # 一般的な単語を除外
        if noun not in common_words:
            nouns.add(noun)

    # カタカナ語（2文字以上）
    katakana_nouns = re.findall(r'[ァ-ヶー]{2,}', text)
    nouns.update(katakana_nouns)

    # よく使われる企業名・製品名（パターン）
    tech_names = re.findall(r'\b(?:OpenAI|Google|Microsoft|Amazon|Meta|Apple|GPT-?\d+|Claude|Gemini|ChatGPT)\b', text, re.IGNORECASE)
    nouns.update([n.strip() for n in tech_names])

    return nouns


def check_speculation_phrases(text: str) -> List[str]:
    """
    推測表現をチェック

    Returns:
        見つかった推測表現のリスト
    """
    speculation_phrases = [
        'かもしれません',
        'かもしれない',
        'と思われます',
        'と思われる',
        'の可能性があります',
        'の可能性がある',
        'と予想されます',
        'と予想される',
        '〜だろう',
        '〜でしょう',
    ]

    found = []
    for phrase in speculation_phrases:
        if phrase in text:
            # 前後の文脈を取得
            matches = re.finditer(re.escape(phrase), text)
            for match in matches:
                start = max(0, match.start() - 30)
                end = min(len(text), match.end() + 30)
                context = text[start:end]
                found.append(f"{phrase}: ...{context}...")

    return found


def check_forbidden_additions(source_summary: str, generated_text: str) -> List[str]:
    """
    元記事にない情報の追加をチェック（ハルシネーション検出）

    注意: 数値チェックは無効化済み。
    記事本文の数値をチェックする意味がないため。
    日付の正確性はPhase 2のLLMチェックで確認する。
    """
    # 数値チェックは無効化（記事本文の数値を弾く意味がないため）
    return []


def fact_check_article(source_item: Dict, generated_html: str) -> Dict:
    """
    生成記事の基本的なファクトチェック

    Args:
        source_item: 元記事の情報 (title, summary, link, etc.)
        generated_html: 生成されたHTML記事

    Returns:
        {
            "passed": bool,  # チェック合格かどうか
            "issues": [問題のリスト],
            "warnings": [警告のリスト]
        }
    """
    issues = []
    warnings = []

    # HTMLタグを除去してテキストのみを取得
    generated_text = re.sub(r'<[^>]+>', ' ', generated_html)
    generated_text = re.sub(r'\s+', ' ', generated_text).strip()

    source_text = f"{source_item.get('title', '')} {source_item.get('summary', '')}"

    # 1. 数値の照合
    source_numbers = extract_numbers(source_text)
    generated_numbers = extract_numbers(generated_text)

    # 元記事の重要な数値が生成記事に含まれているかチェック
    for num in source_numbers:
        if num not in generated_text:
            # 数値の変換をチェック（例：3.5を「3.5」や「約3.5」）
            if not re.search(rf'[約およそ]?\s*{re.escape(num)}', generated_text):
                warnings.append(f"元記事の数値 '{num}' が見つかりません")

    # 元記事にない数値の追加をチェック
    added_nums = check_forbidden_additions(source_text, generated_text)
    if added_nums:
        issues.extend(added_nums)

    # 2. 日付の照合
    source_dates = extract_dates(source_text)
    generated_dates = extract_dates(generated_text)

    # 元記事の日付が正確に含まれているかチェック
    for date in source_dates:
        if date not in generated_text:
            issues.append(f"元記事の日付 '{date}' が正確に記載されていません")

    # 3. 固有名詞の照合
    source_nouns = extract_proper_nouns(source_text)
    generated_nouns = extract_proper_nouns(generated_text)

    # 重要な固有名詞がすべて含まれているか
    important_nouns = [n for n in source_nouns if len(n) > 2]  # 3文字以上
    for noun in important_nouns:
        if noun not in generated_text:
            # 大文字小文字を無視して再チェック
            if noun.lower() not in generated_text.lower():
                warnings.append(f"固有名詞 '{noun}' が見つかりません")

    # 4. 推測表現のチェック
    speculations = check_speculation_phrases(generated_text)
    if speculations:
        # 元記事にも推測表現があるかチェック
        source_speculations = check_speculation_phrases(source_text)
        if len(speculations) > len(source_speculations) + 2:  # 2つ以上多い場合
            warnings.append(f"推測表現が多く含まれています（{len(speculations)}箇所）")

    # 5. 記事の最小文字数チェック
    if len(generated_text) < 500:
        issues.append(f"記事が短すぎます（{len(generated_text)}文字）")

    # 6. タイトルと本文の一貫性チェック
    title_match = re.search(r'<h1[^>]*>(.*?)</h1>', generated_html, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
        # タイトルに含まれる重要な語が本文にも含まれているか
        title_words = [w for w in re.findall(r'[ァ-ヶー]{2,}|[A-Z][a-z]+', title) if len(w) > 2]
        for word in title_words[:3]:  # 最初の3つの重要語をチェック
            if word not in generated_text:
                warnings.append(f"タイトルの '{word}' が本文で説明されていません")

    # 7. FAQセクションの存在チェック
    faq_section = re.search(r'よくある質問', generated_html, re.IGNORECASE)
    if not faq_section:
        issues.append("FAQセクション（よくある質問）が見つかりません")
    else:
        # Q1, Q2, Q3 の存在チェック
        for i in range(1, 4):
            q_pattern = re.compile(rf'Q{i}\.', re.IGNORECASE)
            a_pattern = re.compile(rf'A{i}\.', re.IGNORECASE)
            if not q_pattern.search(generated_html):
                issues.append(f"FAQ質問{i}（Q{i}）が見つかりません")
            if not a_pattern.search(generated_html):
                issues.append(f"FAQ回答{i}（A{i}）が見つかりません")

    # 判定
    passed = len(issues) == 0

    return {
        "passed": passed,
        "issues": issues,
        "warnings": warnings,
        "details": {
            "source_numbers": list(source_numbers),
            "generated_numbers": list(generated_numbers),
            "source_dates": list(source_dates),
            "generated_dates": list(generated_dates),
            "character_count": len(generated_text)
        }
    }


def llm_fact_check_article(source_item: Dict, generated_html: str, client) -> Dict:
    """
    LLMを使用した高度なファクトチェック（Phase 2）

    Args:
        source_item: 元記事の情報
        generated_html: 生成されたHTML記事
        client: Anthropic client

    Returns:
        {
            "passed": bool,
            "score": int (0-100),
            "issues": [問題のリスト],
            "analysis": {
                "logical_consistency": int (0-100),
                "contextual_accuracy": int (0-100),
                "tone_consistency": int (0-100),
                "information_completeness": int (0-100),
                "semantic_accuracy": int (0-100)
            }
        }
    """
    # HTMLタグを除去
    generated_text = re.sub(r'<[^>]+>', ' ', generated_html)
    generated_text = re.sub(r'\s+', ' ', generated_text).strip()

    source_text = f"{source_item.get('title', '')} {source_item.get('summary', '')}"

    # LLMに分析を依頼
    from datetime import datetime
    today = datetime.now().strftime("%Y年%m月%d日")

    system_prompt = f"""あなたは記事品質チェッカーです。

【重要な前提情報】
- 今日の日付: {today}
- 元記事に含まれる製品名・技術名は実在するものとして扱ってください
- 元記事が最新ニュースのため、あなたの知識にない新製品・新サービスが含まれている可能性があります

生成された記事の品質を以下の観点で分析してください。
※注意：元記事の要約を詳しく解説するのがこの記事の目的です。
分析・解説・背景説明の追加は許容されます。

【チェック項目】

1. 論理的一貫性 (logical_consistency)
   - 記事内で矛盾する主張がないか
   - 因果関係が論理的に正しいか
   - 前半と後半で言っていることが矛盾していないか

2. 事実の正確性 (factual_accuracy)
   - 明らかに間違った事実がないか（例：存在しない製品名、明らかに間違った日付）
   - 元記事の核心的な情報が歪曲されていないか
   ※解説や背景説明の追加は問題なし

3. 記事の完全性 (completeness)
   - 記事が途中で切れていないか
   - 文章が途中で終わっていないか
   - 各セクションが完結しているか

4. 内部整合性 (internal_coherence)
   - タイトルと本文の内容が一致しているか
   - リード文と本文の内容が一致しているか
   - 見出しとその下の内容が一致しているか

5. 読みやすさ (readability)
   - 文章として自然か
   - 専門用語に適切な説明があるか
   - 読者が理解できる構成になっているか

【重要】以下は問題としてカウントしないでください：
- 元記事にない背景情報の追加
- 元記事にない技術的解説の追加
- 元記事にない影響分析の追加
- 推測や予測の記述（明示されている場合）

各項目を0-100点で評価し、JSON形式で返してください。
60点未満の項目がある場合は、その理由を詳しく説明してください。"""

    user_prompt = f"""【元記事】
タイトル: {source_item.get('title', '')}
要約: {source_item.get('summary', '')}

【生成記事（HTMLタグ除去済み）】
{generated_text[:6000]}

上記の生成記事を分析し、以下のJSON形式で返してください：

{{
  "logical_consistency": <0-100の整数>,
  "factual_accuracy": <0-100の整数>,
  "completeness": <0-100の整数>,
  "internal_coherence": <0-100の整数>,
  "readability": <0-100の整数>,
  "issues": [
    "問題点1（ある場合のみ）",
    "問題点2（ある場合のみ）"
  ],
  "summary": "総合評価のサマリー"
}}

【重要な注意】
- 背景説明、技術解説、影響分析の追加は問題ではありません
- 元記事にない情報の追加は問題ではありません
- 明らかな事実誤認、論理矛盾、記事の途切れのみを問題としてください

注意：JSONのみを返し、他のテキストは含めないでください。"""

    try:
        # model_helperをインポート
        import sys
        from pathlib import Path
        sys.path.append(str(Path(__file__).parent))
        from model_helper import create_message_with_fallback

        msg = create_message_with_fallback(
            client,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1500,
            temperature=0.0,
            timeout=20.0  # 20秒でタイムアウト
        )

        response_text = "".join([p.text for p in msg.content if p.type == "text"]).strip()

        # JSONをパース
        import json
        # コードブロックマーカーを除去
        response_text = re.sub(r'```json\s*', '', response_text)
        response_text = re.sub(r'```\s*$', '', response_text)

        analysis = json.loads(response_text)

        # スコアを計算
        scores = [
            analysis.get("logical_consistency", 0),
            analysis.get("factual_accuracy", 0),
            analysis.get("completeness", 0),
            analysis.get("internal_coherence", 0),
            analysis.get("readability", 0)
        ]
        average_score = sum(scores) / len(scores)

        # 60点未満の項目があるか、平均が70点未満なら不合格
        min_score = min(scores)
        passed = min_score >= 60 and average_score >= 70

        return {
            "passed": passed,
            "score": int(average_score),
            "min_score": min_score,
            "issues": analysis.get("issues", []),
            "summary": analysis.get("summary", ""),
            "analysis": {
                "logical_consistency": analysis.get("logical_consistency", 0),
                "factual_accuracy": analysis.get("factual_accuracy", 0),
                "completeness": analysis.get("completeness", 0),
                "internal_coherence": analysis.get("internal_coherence", 0),
                "readability": analysis.get("readability", 0)
            }
        }

    except Exception as e:
        print(f"LLMファクトチェックエラー: {e}")
        # タイムアウトや予期しないエラーの場合は不合格として扱う
        # （次の候補記事を試すため）
        return {
            "passed": False,
            "score": 0,
            "min_score": 0,
            "issues": [f"LLMチェックエラー: {str(e)}"],
            "summary": "エラーのため不合格（次の候補を試行）",
            "analysis": {
                "logical_consistency": 0,
                "factual_accuracy": 0,
                "completeness": 0,
                "internal_coherence": 0,
                "readability": 0
            }
        }


def print_fact_check_result(result: Dict) -> None:
    """
    ファクトチェック結果を見やすく表示
    """
    print("\n" + "="*60)
    print("ファクトチェック結果")
    print("="*60)

    if result["passed"]:
        print("✅ 合格")
    else:
        print("❌ 不合格")

    if result["issues"]:
        print("\n【重大な問題】")
        for i, issue in enumerate(result["issues"], 1):
            print(f"  {i}. {issue}")

    if result["warnings"]:
        print("\n【警告】")
        for i, warning in enumerate(result["warnings"], 1):
            print(f"  {i}. {warning}")

    print("\n【詳細】")
    details = result["details"]
    print(f"  文字数: {details['character_count']}")
    print(f"  元記事の数値: {details['source_numbers']}")
    print(f"  生成記事の数値: {details['generated_numbers']}")
    print(f"  元記事の日付: {details['source_dates']}")
    print(f"  生成記事の日付: {details['generated_dates']}")

    print("="*60 + "\n")


def print_llm_fact_check_result(result: Dict) -> None:
    """
    LLMファクトチェック結果を見やすく表示
    """
    print("\n" + "="*60)
    print("品質チェック結果（Phase 2）")
    print("="*60)

    if result["passed"]:
        print(f"✅ 合格（スコア: {result['score']}/100）")
    else:
        print(f"❌ 不合格（スコア: {result['score']}/100, 最低点: {result['min_score']}/100）")

    print("\n【詳細スコア】")
    analysis = result["analysis"]
    print(f"  論理的一貫性: {analysis.get('logical_consistency', 0)}/100")
    print(f"  事実の正確性: {analysis.get('factual_accuracy', 0)}/100")
    print(f"  記事の完全性: {analysis.get('completeness', 0)}/100")
    print(f"  内部整合性: {analysis.get('internal_coherence', 0)}/100")
    print(f"  読みやすさ: {analysis.get('readability', 0)}/100")

    if result["issues"]:
        print("\n【指摘事項】")
        for i, issue in enumerate(result["issues"], 1):
            print(f"  {i}. {issue}")

    if result.get("summary"):
        print(f"\n【総合評価】\n  {result['summary']}")

    print("="*60 + "\n")
