import io
import os
import sys
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from analysis.analyst import Analyst
from ai.predictor import Predictor
from sns.poster import Poster


class Baron:
    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.analyst = Analyst()
        self.predictor = Predictor()
        self.poster = Poster()

    def run(self, query: str):
        print("\n🎩 バロン: 各部署に指示を出します...\n")

        print("📊 分析部 稼働中...")
        analysis_report = self.analyst.analyze(query)

        print("🤖 AI予測部 稼働中...")
        ai_report = self.predictor.predict(query)

        print("📣 SNS部 稼働中...")
        sns_report = self.poster.draft(query)

        print("\n🎩 バロン: 報告をまとめます...\n")
        summary = self._summarize(query, analysis_report, ai_report, sns_report)

        print("=" * 60)
        print("【バロン最終報告】")
        print("=" * 60)
        print(summary)
        print("=" * 60)

    def _summarize(self, query, analysis, prediction, sns):
        response = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1200,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたはSCI（走破コンフォート指数）の最高責任者バロンです。"
                        "分析部・AI予測部・SNS部の3部署からの報告を受け取り、"
                        "ユーザーへ簡潔かつ的確にまとめて報告してください。"
                        "口調は落ち着いたプロフェッショナルで、要点を箇条書きで整理してください。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"依頼内容: {query}\n\n"
                        f"【分析部の報告】\n{analysis}\n\n"
                        f"【AI予測部の報告】\n{prediction}\n\n"
                        f"【SNS部の報告】\n{sns}"
                    ),
                },
            ],
        )
        return response.choices[0].message.content


def main():
    if not os.getenv("GROQ_API_KEY"):
        print("エラー: GROQ_API_KEY が設定されていません。")
        print("例: set GROQ_API_KEY=gsk_...")
        sys.exit(1)

    baron = Baron()

    print("🎩 バロン起動。何でもお申し付けください。（終了: exit）")
    while True:
        query = input("\nあなた: ").strip()
        if not query:
            continue
        if query.lower() == "exit":
            print("🎩 バロン: お疲れ様でした。")
            break
        baron.run(query)


if __name__ == "__main__":
    main()
