# 다중신호 융합 IP 측위 엔진 (연구용 프로토타입)

여러 독립 신호를 **위치추정값 + 불확실성반경 + 신뢰가중치**로 정규화해 융합하는 IP 지오로케이션 프로토타입입니다. 단일 소스 GeoIP의 한계(같은 IP를 두 업체가 다르게 응답하는 문제 등)를 다중 신호 교차검증으로 보완합니다.

> 🔬 **연구용 프로토타입입니다. 은닉/기만 수집을 하지 않습니다.** GPS 데이터는 명시적으로 동의한 사용자에게서만 수집합니다.

## 무엇을 하나

### 1) 단일 IP 정밀 측위
한 IP를 5개 신호로 동시에 분석하고, 지도에 신호별 근거(불확실성 원)와 융합 결과(신뢰원)를 표시합니다.

| 신호 | 데이터원 | 정밀도 |
|------|----------|--------|
| ① 다중 GeoIP | ip-api.com + ipapi.co | 도시급 (~수십 km) |
| ② ASN 분류/게이팅 | ip-api 플래그(mobile/hosting/proxy) | 위치 아님 — 신뢰도 조정 |
| ③ Traceroute 힌트 | 로컬 `tracert` 호스트명의 IATA/도시 코드 | 광역 (~수백 km) |
| ④ 레이턴시 삼각측량 | Globalping 다지점 실측 ping | 조대 (국가/대륙) |
| ⑤ 크라우드소싱 GPS | 동의 기여 DB (DBSCAN+시계열가중) | **미터~수백 m (정밀 핵심)** |

융합은 **계층형 + 역분산 가중**입니다: 모바일이면 조기차단, 동의 GPS 군집이 있으면 그것이 지배, 없으면 ①③④를 역분산 가중 융합하고 ②의 플래그가 신뢰도를 깎습니다.

### 2) 동의 기여 모드 (`/contribute`)
목적·수집항목·보관·삭제권을 고지하고, 명시적 동의 체크 후에만 브라우저 GPS를 공인 IP와 매핑해 저장합니다. 본인 데이터 삭제 가능.

## 실행

```bash
# (선택) 가상환경
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt

# 프로젝트 루트에서
uvicorn app.main:app --reload
# http://127.0.0.1:8000  (조회) / http://127.0.0.1:8000/contribute  (기여)
```

필요 패키지: `fastapi`, `uvicorn`, `httpx`, `numpy` (DBSCAN은 scikit-learn 없이 numpy로 직접 구현).
외부 연동: ip-api.com·ipapi.co(GeoIP, 무료/무키), Globalping(다지점 실측, 무료/무키, 레이트리밋 있음), 지도 타일 OpenStreetMap.

## 정확도와 한계 (정직하게)

- ③ traceroute, ④ 레이턴시는 **광역까지만** 좁힙니다(수십~수백 km). 라우터가 호스트명을 숨기거나 ping에 응답하지 않으면 해당 신호는 비게 됩니다.
- **진짜 정밀도는 ⑤ 동의 GPS가 쌓여야** 나옵니다. 그 전까지 결과는 도시/광역 수준이며 UI에 신뢰반경으로 솔직히 표기됩니다.
- ② 모바일(CGNAT) 대역은 개인 단위 측위가 **원천적으로 불가**하므로 조기 차단합니다.

## 프라이버시 · 법적 고지 (중요)

이 프로토타입은 **합법적·투명한 동의 기반 수집만** 구현합니다. 다음을 반드시 유의하세요.

- **위치정보의 보호 및 이용 등에 관한 법률**: 개인위치정보를 수집·이용하려면 방송통신위원회(KCC)에 **위치정보사업/위치기반서비스사업 허가·신고**가 필요하고, **명시적·구체적 동의**가 있어야 합니다. 이용자 모르게 수집하는 은닉/기만 방식은 **형사처벌 대상**입니다.
- **개인정보보호법(PIPA)**: 공인 IP + 정밀 GPS는 개인정보입니다. 적법근거·목적특정·고지·동의·목적제한·삭제권을 준수해야 합니다.
- **브라우저 정책**: Geolocation 권한은 HTTPS + 사용자 제스처가 필요하며, 권한 남용 사이트는 자동 차단됩니다.

**실제 서비스로 배포하려면** 위 법적 절차(KCC 신고, 동의 설계, 개인정보 처리방침, 보관기간·파기 정책)를 먼저 갖춰야 합니다. 본 저장소는 기술 시연용이며 그대로 운영하면 안 됩니다.

> **리버스 프록시 뒤에 배포할 때만** `GEOIP_TRUST_PROXY=1` 환경변수를 설정하세요. 기본값은 꺼짐입니다 — 켜지 않으면 `X-Forwarded-For` 헤더를 신뢰하지 않아(소켓 IP만 사용) 헤더 위조로 타인 데이터를 삭제하는 우회를 막습니다. 신뢰할 수 있는 프록시가 실제 클라이언트 IP를 넣어줄 때만 켜야 합니다.

## 구조

```
app/
  main.py            FastAPI 라우트 + 측위 파이프라인
  models.py          공통 데이터 계약 (Estimate, ProviderResult, Classification, LocateResult)
  geo.py             지오 수학 (haversine, numpy-DBSCAN, 삼각측량, 역분산 융합)
  store.py           동의 기여 SQLite 저장소
  globalping.py      Globalping 측정 클라이언트
  fusion.py          계층형 역분산 융합
  signals/           geoip · asn · traceroute · latency · crowdsource
data/iata.csv        호스트명 IATA 힌트용 공항 좌표
frontend/            index.html(조회) · contribute.html(동의 기여)
docs/specs/          설계 스펙
```
