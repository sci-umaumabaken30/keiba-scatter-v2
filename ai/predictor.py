import anthropic
import os


class Predictor:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def predict(self, query: str) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=(
                "あなたはSCI（走破コンフォート指数）のAI予測部です。"
                "クッション値・含水率との相性(50%)・脚質バイアス(30%)・近走脚質バイアス(20%)を元に"
                "SCIポイントを算出し、単勝・馬連・3連複の推奨馬券を報告してください。"
                "現時点ではデータ接続前のため、予測ロジックと必要なデータを報告してください。"
            ),
            messages=[{"role": "user", "content": query}],
        )
        return response.content[0].text
