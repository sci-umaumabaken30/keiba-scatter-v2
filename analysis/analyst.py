import anthropic
import os


class Analyst:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def analyze(self, query: str) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=(
                "あなたはSCI（走破コンフォート指数）の分析部です。"
                "クッション値・含水率・脚質バイアスなど馬場データの傾向を分析し、簡潔に報告してください。"
                "現時点ではデータ接続前のため、分析の観点と必要なデータを報告してください。"
            ),
            messages=[{"role": "user", "content": query}],
        )
        return response.content[0].text
