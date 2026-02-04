# 🐇 Rabbit System V3 - OCI Server Documentation
> 마지막 업데이트: 2026-01-27
> 작성자: Admin

## 1. 프로젝트 개요
- **목적**: 사내 PC에서 안전하게 소설을 열람하기 위한 중계 시스템
- **아키텍처**: 
  - 🏢 **Client (회사)**: 요청 및 조회 (SSH 터널링 사용)
  - ☁️ **Hub (OCI)**: API 서버 및 DB (중계소)
  - 🏠 **Worker (집)**: 실제 크롤링 및 데이터 전송

## 2. 디렉토리 구조
- **위치**: `/home/ubuntu/workspace/rabbit-system`
- **구성**:
  - `docker-compose.yml`: 인프라 설정
  - `api_server/`: Python FastAPI 서버 소스코드
  - `pgdata/`: DB 데이터 저장소 (자동 생성됨)

## 3. 서비스 포트 및 접속 정보 (중요!)

### 🔌 포트 정보
| 서비스 | 내부 포트 | **외부 포트 (호스트)** | 용도 |
| :--- | :--- | :--- | :--- |
| **API Server** | `http://144.24.87.146:8001` | /client/list, /worker/get |
| **pgAdmin** | `http://144.24.87.146:5050` |  |
| **Portainer** | `http://144.24.87.146:9000` |  |


### 🔑 계정 정보 (Credentials)
**[Database: PostgreSQL]**
- **DB Name**: `rabbit_novel`
- **User**: `root`
- **Password**: `neko15746+`

**[SSH 접속]**
- **User**: `ubuntu`
- **Key File**: (본인 PC에 저장된 .key 파일 사용)

## 4. 관리 명령어 (Docker)

### ▶️ 실행 및 재시작

# 수정 사항 반영하여 재빌드 후 실행
sudo docker compose up -d --build

# 단순 재시작
sudo docker compose restart

### ⏹️ 종료 및 초기화
# 실행 중인 컨테이너 종료
sudo docker compose down

# [주의] DB 데이터까지 싹 날리고 초기화 (에러 날 때 사용)
sudo docker compose down -v

### 📜 로그 확인
# API 서버 로그 (실시간)
sudo docker logs -f rabbit_api

# DB 로그
sudo docker logs -f rabbit_db

### 5. API 엔드포인트 테스트 (Curl)
# 상태 확인
curl http://localhost:8001/

# [Client] 소설 목록 조회
curl http://localhost:8001/client/list

# [Worker] 일감 조회
curl http://localhost:8001/worker/get

### 6. 기타 메모
# SSH 터널링 예시 (회사 PC CMD):
ssh -i key.key -L 9999:localhost:8001 ubuntu@OCI_IP

# 집 노트북 설정:
config.txt에 http://OCI_IP:8001 입력 필수.

### 💡 팁: 나중에 확인하는 법
나중에 접속하셔서 기억이 안 날 때마다 아래 명령어를 치시면 됩니다.

# 내용 출력
cat README.md

# 내용이 길어서 스크롤하며 보고 싶을 때 (나가기는 q)
less README.md