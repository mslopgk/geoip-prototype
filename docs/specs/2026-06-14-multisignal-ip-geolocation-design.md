# 다중신호 융합 IP 측위 엔진 — 설계 스펙

- 날짜: 2026-06-14
- 상태: 승인됨 (사용자 승인 후 빌드 진행)
- 스택: Python 3.12 / FastAPI / httpx / numpy / SQLite / Leaflet(OSM)

## 1. 목적과 범위

기존 단일 소스 GeoIP의 한계를 극복하기 위해, 여러 독립 신호를 **위치추정값 + 불확실성반경 + 신뢰가중치**로 정규화해 융합하는 IP 측위 프로토타입. 두 가지 모드를 모두 제공한다.

- **단일 IP 정밀 측위**: 한 IP를 모든 신호로 융합 추정하고 지도에 신호별 근거와 융합 결과를 표시.
- **동의 기여 모드**: 방문자가 명시적으로 동의한 경우에만 브라우저 GPS를 IP와 매핑해 누적, 크라우드소싱 정밀도 층을 형성.

### 비범위 / 금지
- 이용자 모르게 위치를 수집하는 은닉·기만('미끼') 설계는 **구현하지 않는다**. 한국 위치정보법(KCC 허가·신고 + 명시 동의), 개인정보보호법, 브라우저 권한 정책 위반.
- 실제 배포 시 위치정보사업/위치기반서비스 신고가 필요함을 README에 경고한다.

## 2. 신호 (원 기획 기법 전부 포함 + Globalping 업그레이드)

| # | 신호 | 데이터원 | 산출 | 정밀도 | 비고 |
|---|------|----------|------|--------|------|
| ① | 다중 GeoIP | ip-api.com, ipapi.co | 독립 추정 2개 | 도시급(~수십 km) | 소스 불일치 감지 |
| ② | ASN 분류/게이팅 | ip-api 플래그 | mobile/hosting/proxy | 위치 아님(가중치) | mobile→조기차단 |
| ③ | Traceroute 힌트 | 로컬 `tracert` | 종단 hop 호스트명 IATA/도시 파싱 → 좌표 | 광역(~수백 km) | 미응답 hop 많음 |
| ④ | 레이턴시 삼각측량 | Globalping 다지점 ping | RTT→최대거리 원반 교집합 | 조대(국가/대륙) | 큰 오류 제거용 |
| ⑤ | 크라우드소싱 GPS | 동의 기여 DB | IP/`/24` 누적 좌표, DBSCAN+시계열가중 | 미터~수백 m | **정밀 핵심** |

## 3. 융합 로직 (fusion.py)

계층형 + 역분산 가중:
1. ②가 mobile이면 → `early_return`, "개인 단위 측위 불가(CGNAT)". 무거운 ③④ 생략.
2. ⑤(동의 GPS 군집)이 존재하면 그것이 지배 — DBSCAN 최대 군집의 시계열가중 중심, 신뢰등급 "정밀(GPS)".
3. 아니면 ①③④를 `inverse_variance_fuse`로 합성. ②의 hosting/proxy 플래그는 신뢰가중치를 깎고 메시지로 경고.
4. ④ 삼각측량은 융합점이 그 원반 밖이면 "물리적으로 모순"을 경고(VPN/프록시 시사).
5. 출력: `LocateResult` — 융합 좌표 + 신뢰반경 + 신뢰등급 + 신호별 estimate 리스트 + 메시지.

## 4. 모듈 계약 (구현 시 정확히 준수)

공통 타입은 `app/models.py`: `Estimate(signal,lat,lon,radius_km,weight,label,meta)`, `ProviderResult`, `Classification`, `LocateResult`.
지오 수학은 `app/geo.py`: `haversine_km`, `rtt_to_max_distance_km`, `dbscan_haversine(points,eps_km,min_samples)`, `triangulate([(lat,lon,maxR_km,weight)])`, `inverse_variance_fuse(estimates)`, `centroid`.
저장소는 `app/store.py`: `init_db`, `add_contribution`, `get_points_for_ip`, `delete_for_ip`, `ipv4_prefix24`, `count`.
Globalping은 `app/globalping.py`: `await measure(client, target, mtype, limit, locations, packets)`, `WORLD_SPREAD`.

구현 대상 모듈 시그니처:
- `signals/geoip.py`: `async def lookup_all(ip, client) -> list[ProviderResult]`; `def to_estimates(providers) -> list[Estimate]`.
- `signals/asn.py`: `def classify(providers) -> Classification`; `def should_early_return(c) -> bool`.
- `signals/traceroute.py`: `async def collect(ip, client) -> list[Estimate]` (로컬 tracert + 호스트명 IATA 파싱, `data/iata.csv` 사용).
- `signals/latency.py`: `async def collect(ip, client) -> list[Estimate]` (Globalping ping → triangulate).
- `signals/crowdsource.py`: `def collect(ip) -> list[Estimate]` (store 조회 + DBSCAN + 시계열가중).
- `fusion.py`: `def fuse(ip, classification, estimates) -> LocateResult`.
- `app/main.py`: 라우트 `/`, `/contribute`, `GET /api/locate`, `POST /api/contribute`, `POST /api/delete`, 정적 서빙.

## 5. API

- `GET /api/locate?ip=<optional>` → `LocateResult` JSON. ip 생략 시 서버 공인 IP 자동 탐지.
- `POST /api/contribute` `{lat,lon,accuracy,tz,lang,consent:true}` → 서버가 공인 IP 캡처 후 저장. 동의 false면 거부.
- `POST /api/delete` → 호출자 IP의 기여 데이터 삭제(잊힐 권리).

## 6. 프론트엔드

- `frontend/index.html`: IP 입력/내 IP 조회, Leaflet+OSM 지도, 신호별 마커+불확실성 원(색 구분), 융합점(강조)+신뢰원, 신호별 근거 패널, 신뢰등급 배지, 경고 메시지. 연구 프로토타입 배너 상시.
- `frontend/contribute.html`: 목적·보관·삭제 고지 → 명시 동의 체크 → Geolocation 요청 → POST. 삭제 버튼.

## 7. 정확도와 한계 (정직 표기)

- ③④는 광역까지만(수십~수백 km). 진짜 정밀도는 ⑤가 쌓여야 나온다. UI/README에 신뢰반경으로 솔직히 표기.
- 라이브 실측: GeoIP 2소스, ASN/플래그, 로컬 traceroute, Globalping 다지점 RTT.

## 8. 프라이버시·법

연구 프로토타입 배너 상시 · 기여는 명시 동의·목적 고지·삭제권 · 은닉수집 없음 · 로컬 SQLite 보관 · 실배포 시 KCC 신고 경고. timezone/language↔IP 교차검증은 기여 유효성 플래그로만 사용(개인 추적 무기로 쓰지 않음).

---

## 9. 진화 (구현 후 변경 이력 · addendum)

> 위 1–8은 2026-06-14 원 설계 기록(보존). 아래는 구현·실측·정확도 강화 과정에서 추가된 변경이며, 모두 **기존 신호/코드를 보존한 가산(additive)** 방식이다.

### 9.1 핵심 발견 (실측)
한국 주거 IP(LG DACOM/POWERCOMM 등)는 **모든 무료 네트워크 신호로 시/구 단위 측위 불가**임을 경험적으로 확정: 무료 GeoIP 7종(ip-api·ipapi.co·ipwho.is·ipapi.is·ip.guide·ipleak·geolocation-db 등) 전부 서울권으로 수백 km 오답, PTR/whois/RDAP에 지역정보 없음(전국 블록), Globalping latency는 남부 KR 프로브 부재 + 라스트마일 지연으로 서울/부산 구분 불가, traceroute는 CGNAT/무명 라우터. → **유일한 네트워크 돌파구는 능동 레이턴시(아래 ⑥)**, 그 외 정밀도는 ⑤ 동의 GPS.

### 9.2 신호 ⑥ 추가 — 능동 레이턴시 (vantage 측위)
`app/signals/anchor_latency.py`. 측정 서버 자신의 위치를 `GEOIP_VANTAGE="lat,lon"`로 설정하면, 그 서버에서 타깃을 OS `ping`으로 직접 측정해 왕복 RTT ≤ `VANTAGE_LOCAL_MS`(5ms)면 타깃이 vantage에 인접(빛의 속도: 300km 회선만으로도 왕복 >3ms → 원거리 물리적 배제)으로 보고 vantage 위치로 측위. 반경 = SoL 상한에서 last-mile(1ms) 차감. **주소 시드가 아니라 RTT 측정으로 획득**. 미설정 시 무음(기존 동작 보존). 안전: 타깃 IP가 포함된 응답 줄의 RTT만 인정(온링크 NAT 응답자 거짓측위 방지), 전역 유니캐스트만 능동측정.

### 9.3 융합 개편 (geo.py / fusion.py)
- 우선순위 **⑤ > ⑥ > ①③④** (crowdsource > anchor 측정 > GeoIP 융합).
- `inverse_variance_fuse`: **de-correlation**(디스크중첩 단일연결로 상관 GeoIP를 1대표로 붕괴 → 클론이 거짓 확신 못 만듦) + **dispersion**(소스 불일치 시 반경↑) + **max_reach 커버리지 바닥**(원본 전체 포함 보장) + 비유한 가드.
- 라벨은 페널티·교차검증 *후* 계산(과신 라벨 방지). latency 물리 모순 시 경고만이 아니라 **반경 확대**.
- GeoIP 단일 출처 결과엔 정직성 경고 메시지.

### 9.4 신호 강화
- ① GeoIP **2→4 제공자**(+ip.guide, +ipleak), `accuracy_radius` 반영. city 반경 25→40km(주거 오차 보정).
- ⑤ crowdsource: 같은 IP 기여 우선(시간감쇠×부스트 가중 군집 선택), GPS 정확도 바닥, sparse 바닥, **/24 prefix 프라이버시 바닥(이웃에게 정밀 노출 금지)**.
- ③ traceroute: 인터페이스 토큰(gig/tun/ae·be·ge…)·국가불일치 IATA 오탐 제거. ④ latency: 비유용(>3000km) 억제.

### 9.5 출력·역지오코딩
`app/geocode.py`: 융합 좌표 → 행정구역명(OSM Nominatim, best-effort·캐시·실패 시 None, 비한국은 display_name). `LocateResult.address`로 노출, 프런트 표시.

### 9.6 보안 하드닝 (보안 리뷰 워크플로 반영)
능동측정 게이트 `_is_global_unicast`(멀티캐스트·CGNAT·reserved·NAT64 차단), `/api/locate` ip 검증(400) + 좌표 검증(NaN/Inf/범위), DoS 완화(결과 캐시 TTL+크기상한, 능동측정 동시성 세마포어), `/api/delete` CSRF 헤더, 자가 IP https, 제공자 에코 재검증.

### 9.7 검증·결과
pytest 68개(유닛+통합+보안), 적대적 자기검증 워크플로 3회로 회귀·안전 버그 수정. **최종 실측(GEOIP_VANTAGE=구서동): 신뢰원 정직성 3/3, 평균 중심오차 211km → 7.8km** (구서동 IP 0.0km/정밀, 명지동 23.5km/부산). 운영: 이 호스트는 8000 예약(WinError 10013) → 9000 사용(UI 상대경로).
