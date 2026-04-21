import os
from groq import Groq


class Predictor:
    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    def predict(self, query: str) -> str:
        response = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=800,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたはSCI（走破コンフォート指数）のAI予測部です。"
                        "クッション値・含水率との相性(50%)・脚質バイアス(30%)・近走脚質バイアス(20%)を元に"
                        "SCIポイントを算出し、単勝・馬連・3連複の推奨馬券を報告してください。"
                        "現時点ではデータ接続前のため、予測ロジックと必要なデータを報告してください。"
                    ),
                },
                {"role": "user", "content": query},
            ],
        )
        return response.choices[0].message.content
