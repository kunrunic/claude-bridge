# claude-bridge 종합 기술 검토 보고서

**검토일**: 2026-04-15  
**검토자**: 12명의 전문가 (설계, Telegram, UX, QA, 네트워크, 개발 × 긍정/비평)  
**대상**: claude-bridge (bot.py 1066줄)

---

## 1. 전체 평가 요약

세션 락 시스템, 비동기 UI 처리, 다중 인스턴스 안전성에서 우수한 설계를 보여주나, **폴링 네트워크 복구 메커니즘 부재, 동기적 subprocess 블로킹, 타임아웃 미설정** 등으로 프로덕션 장기운영에 위험 요소가 있습니다. 현재 상태로는 "단일 사용자, 단일 폴더" 용도로 안정적이며, 멀티 인스턴스 및 장기 운영에는 Phase 1 수정이 필요합니다.

**최종 평가**: 개념 설계 9/10 | 구현 품질 7/10 | 운영 준비도 6/10

---

## 2. 핵심 강점 TOP 5

### 1. 멀티 인스턴스 세션 락 시스템의 원자성 (Design, Dev, Network, QA)
`open("x")` exclusive create로 race condition 방지, stale lock 자동 감지/정리, chat_id 소유권 추적.  
**코드**: `bot.py:226-245`

### 2. 비동기 이벤트 핸들링 + 모니터 태스크 분리 (Telegram, Dev)
`asyncio.create_task()`로 monitor를 daemon처럼 관리, busy 진입/지속/종료 edge 감지.  
**코드**: `bot.py:484-692`, `bot.py:877-883`

### 3. 사용자 경험 중심의 상태 피드백 (UX, Telegram)
⏳ 경과 시간 갱신, ✅/❌ 승인 확정, 사용량 한도 리셋 시간 파싱.  
**코드**: `bot.py:620-641`, `bot.py:553-555`

### 4. 정교한 상태 감지 정규식 (Design, Telegram)
5단계 상태 분류(APPROVAL/TRUST/BUSY/LIMIT/STATUS), ANSI 제거 후 정규화.  
**코드**: `bot.py:56-76`

### 5. 높은 테스트 커버리지 (QA)
76개 테스트, stale lock/race condition/비동기 흐름/tmux 죽음 포괄, 알려진 버그 문서화.  
**코드**: `tests/` 전체

---

## 3. 즉시 수정 필요 (HIGH)

### H1. Telegram API 폴링 재연결 실패 미처리
**문제**: 네트워크 오류 후 자동 복구 없음 → 봇 좀비 상태  
**코드**: `bot.py:1062`  
**해결**: exponential backoff 재시도, 최종 실패 시 `sys.exit(1)` (자동 재시작 감지)  
**언급**: Network 비평, Dev 비평

### H2. Subprocess 동기 호출이 asyncio 블로킹
**문제**: `subprocess.run()` 400ms~1s 블로킹 → 다른 핸들러 지연  
**코드**: `bot.py:80-82` (`tmux_run`)  
**해결**: `loop.run_in_executor()` 또는 `asyncio.create_subprocess_exec()`, timeout=5.0  
**언급**: Dev 비평, Network 비평

### H3. 타임아웃 설정 전반 부재
**문제**: tmux 무한 대기 가능, Telegram Request 타임아웃 없음  
**코드**: `bot.py:80-82`, `bot.py:1062`  
**해결**: `subprocess.run(timeout=5.0)`, `Request(connect_timeout=10, read_timeout=15)`  
**언급**: Network 비평, Dev 비평

### H4. 글로벌 싱글톤 bridge의 비동기 race condition
**문제**: `was_busy`, `awaiting_approval` 등 비동기-unsafe 상태 공유  
**코드**: `bot.py:695`, `bot.py:385-450`  
**해결**: `asyncio.Lock` 도입, BridgeState dataclass로 상태 접근 직렬화  
**언급**: Dev 비평, Design 비평

### H5. asyncio.get_event_loop() deprecated (테스트)
**문제**: Python 3.10+ 경고, 3.13에서 제거 예정  
**코드**: `tests/test_scenarios.py`, `tests/test_concurrent_commands.py`, `tests/test_tmux_death.py`  
**해결**: `asyncio.run()` 또는 `pytest-asyncio` 도입  
**언급**: QA 비평

---

## 4. 중기 개선 사항 (MED)

| # | 항목 | 코드 위치 | 해결책 | 언급 |
|---|------|---------|--------|------|
| M1 | Bare except 과다 사용 | bot.py 전반 | 구체적 Exception + logging | Dev, QA |
| M2 | 함수 내부 import | bot.py:40,90,531 | 파일 상단으로 이동 | Dev |
| M3 | 하드코딩 매직 넘버 | bot.py:86,288,300 | 상수 정의 (BUFFER_LINES, MAX_CHUNKS 등) | Dev |
| M4 | CancelledError 미처리 | bot.py:445,474 | `except asyncio.CancelledError: raise` 명시 | Dev |
| M5 | find_sessions I/O 블로킹 | bot.py:288-314 | ThreadPoolExecutor 병렬 파싱 | Dev, Network |
| M6 | Config 로드 오류 처리 부재 | bot.py:27-30 | 파일/JSON/필드 검증, sys.exit(1) | Dev |
| M7 | 혼동 유발 오류 메시지 | bot.py:965 | "이미 사용 중인 세션입니다. /unlock 으로 해제하세요." | UX |
| M8 | /unlock 발견성 낮음 | bot.py:965 | 잠금 오류 메시지에 /unlock 버튼 포함 | UX |

---

## 5. 장기 고려사항 (LOW/아키텍처)

### L1. bot.py 모듈 분리
1066줄 모놀리스 → `lock_manager.py`, `session_monitor.py`, `tmux_bridge.py`, `telegrambot.py`

### L2. JSON 구조화 로깅
포그라운드 출력 → RotatingFileHandler + JSON 포맷 (session_id, chat_id, duration_ms 포함)

### L3. 멀티 유저 설계
chat_id별 Bridge 풀 또는 "1 인스턴스 = 1 사용자" 명시적 문서화

### L4. NFS 파일 락 안전성
`open("x")`는 NFS v3 미보장 → `fcntl.flock()` 또는 Redis 분산 락 검토

---

## 6. 전문가별 최우선 권고사항

| 전문가 | 긍정 핵심 | 비평 핵심 |
|-------|---------|---------|
| **설계** | 세션 락 원자성, 관심사 분리 명확 | 모놀리식 구조, 멀티 유저 미지원 |
| **Telegram** | Inline keyboard UX, reaction 피드백 | Rate limit 부재, drop_pending 메시지 유실 |
| **UX** | ⏳ 상태 피드백, 80col 모바일 친화 | "다른 인스턴스" 혼동 메시지, /unlock 발견성 낮음 |
| **QA** | 높은 커버리지, 회귀 테스트 문서화 | asyncio.get_event_loop() deprecated, 모듈 격리 실패 |
| **Network** | 폴링 방화벽 친화, stale lock 정리 | 재연결 미처리, NFS 락 race, 타임아웃 부재 |
| **개발** | 원자성 보장, 상태 머신 정교함 | Blocking subprocess, bare except 남용, 타입 힌트 부족 |

---

## 7. 다음 스프린트 액션 아이템

### Phase 1 — 긴급 (1주)
1. `tmux_run_async()` 구현 — `run_in_executor` + timeout=5.0
2. Polling 재연결 루프 — exponential backoff, fatal 시 sys.exit(1)
3. Monitor `CancelledError` 명시 처리
4. Monitor error_count 도입 — 10회 연속 실패 시 Telegram 알림
5. Telegram `Request` 타임아웃 설정

### Phase 2 — 우선 (2주)
6. 테스트 `asyncio.run()` 마이그레이션 + pytest-asyncio
7. Bare except → 구체적 Exception + 로깅
8. 잠금 오류 메시지 UX 개선 + /unlock 버튼화
9. Config 로드 검증 강화
10. 상수 정의화 (매직 넘버 제거)

### Phase 3 — 선택 (1개월+)
- JSON 로깅, 모듈 분리, NFS 락 개선, 멀티 유저 설계 검토

---

## 권장 배포 조건

- ✅ 단일 사용자, 로컬 ext4/APFS 파일시스템 (NFS 금지)
- ✅ systemd 또는 launchd 자동 재시작 메커니즘 필수
- ⚠️ Phase 1 H1~H3 최소 수정 후 장기 운영 권고
