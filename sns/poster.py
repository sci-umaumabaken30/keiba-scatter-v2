import anthropic
import os


class Poster:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def draft(self, query: str) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=(
                "あなたはSCI（走破コンフォート指数）のSNS運用部です。"
                "分析・予測結果をもとにX（Twitter）用の投稿下書きを作成してください。"
                "140文字以内・ハッシュタグあり・手動確認前提の下書きとして出力してください。"
                "現時点ではデータ接続前のため、投稿テンプレートの案を報告してください。"
            ),
            messages=[{"role": "user", "content": query}],
        )
        return response.content[0].text
