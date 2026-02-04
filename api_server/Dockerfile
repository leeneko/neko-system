FROM python:3.9-slim

WORKDIR /app

# git 설치 (PyKoSpacing 설치용)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# [수정됨] requirements.txt 파일을 복사하고 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 나머지 소스코드 복사
COPY . .

# 실행 명령어 (docker-compose에서 덮어쓰긴 하지만 명시)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
