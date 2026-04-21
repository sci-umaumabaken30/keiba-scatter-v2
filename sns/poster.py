import os
from groq import Groq


class Poster:
    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    def draft(self, query: str) -> str:
        response = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=800,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたはSCI（走破コンフォート指数）のSNS運用部です。"
                        "分析・予測結果をもとにX（Twitter）用の投稿下書きを作成してください。"
                        "140文字以内・ハッシュタグあり・手動確認前提の下書きとして出力してください。"
                        "現時点ではデータ接続前のため、投稿テンプレートの案を報告してください。"
                    ),
                },
                {"role": "user", "content": query},
            ],
        )
        return response.choices[0].message.content
