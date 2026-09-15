FROM python:3.12-slim

# claude_agent_sdk가 내부적으로 claude CLI를 실행한다. Linux npm 설치본은 평범한 셸
# 스크립트라 SDK가 바로 실행할 수 있다(fabot-daily-briefing-agent와 동일한 방식).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm curl \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# root로 실행하면 claude CLI가 --dangerously-skip-permissions를 보안상 거부한다.
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

ENV PORT=8766
EXPOSE 8766
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
