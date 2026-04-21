import os
from groq import Groq


class Analyst:
    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    def analyze(self, query: str) -> str:
        response = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=800,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたはSCI（走破コンフォート指数）の分析部です。"
                        "クッション値・含水率・脚質バイアスなど馬場データの傾向を分析し、簡潔に報告してください。"
                        "現時点ではデータ接続前のため、分析の観点と必要なデータを報告してください。"
                    ),
                },
                {"role": "user", "content": query},
            ],
        )
        return response.choices[0].message.content
