# 通用 Dockerfile —— 同时兼容 Hugging Face Spaces(Docker SDK) 与 Render(Docker)
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 确保数据目录可写（选题库持久化）
RUN mkdir -p /app/data && chmod -R 777 /app/data

ENV PORT=7860
ENV HOST=0.0.0.0

EXPOSE 7860

CMD ["python", "app.py"]
