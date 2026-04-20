# Reliability/Ops 긍정 평가 — Pipe-Pane Redesign Step 3

## 요약

파이프라인 기반 설계는 기존 스냅샷 방식의 근본적 한계를 구조적으로 해결한다. 특히 **offset 기반 dedup**, **라인 커밋 판정**, **alt-screen 분리** 등 3가지 핵심 메커니즘이 지난 3개월 운영 중 반복된 회귀 계열(eviction, stale boundary, echo 오탐)을 대부분 예방한다.

---

## 1. Offset 기반 Dedup 및 복구 가능성

**현상황 (기존 parser.py)**:
- `StreamQueue.idx` + `last_fp` (content hash): 500줄 스크롤백 eviction 시 내용이 사라져도 idx 는 그대로 → `idx > completed_count` 고착 (bugreport/20260417_095801 의 live evidence, 23회 eviction epoch).
- 복구 불가: rotated file 의 내용을 다시 볼 수 없음.

**신설계 (step-3)**:
- **byte offset** (session 시작 이후 누적): 파일 rotation 이후에도 이어짐. VT state 를 replay 할 수 있음.
- **inode 감지** (side-effects.md §R1): rotate 시 이전 파일 inode 를 기록 → 재기동 시 last_offset 으로 중단점 재개 (§6 리스크 표).
- **결과**: eviction 이후에도 분석기가 offset 을 진행하므로 deadlock 불가능. `--until-offset N` CLI 옵션으로 사후 재분석도 가능.

**근거 문서**: step-3-design.md §1 (전환 전제), §8 (관측성 표준), step-2-results.md (offset-based resume 언급).

---

## 2. LineCommitter 가 eviction 명시화 (R2 rule)

**현상황**: 
- pane 에서 블록이 scrollback 밖으로 밀려나는 순간 포착 불가.
- 결과적으로 "아까 나온 블록이 왜 없지?" 진단이 불가능.

**신설계**:
- LineCommitter.R2 rule: `VTScreen.evicted_lines` → `scroll_evict` reason commit.
- 이벤트 스트림에 `{"offset": X, "t": "block_commit", "reason": "scroll_evict", "text": "..."}`로 기록.
- **관측성**: eviction 이 언제 일어났는지 정확히 알 수 있음.
- **대응**: 나중에 "offset X ~ Y 구간에서 block N이 소실됐다" → `--until-offset Y` 로 상태 재현 가능.

**근거**: tools/line_commit.py:56-73 (R2 구현), step-3-design.md §8 (offset+json 로깅).

---

## 3. 라이브 모달 판정 게이트 (Fix 1 원리)

**현상황** (20260418_094744):
- `_response_region` 이 **전체 pane 역방향 스캔** → ESC 로 취소된 모달의 텍스트가 scrollback 에 남아있으면 매칭 → `end` 를 모달 앞 divider 로 고정 → 이후 모든 신규 블록이 범위 밖으로 밀려남.
- **문제**: `is_approval(text)` 는 tail 60줄 제한을 두지만 경계 탐색은 무제한 → 정책 불일치.

**신설계 (step-3 설계의 방향)**:
- `_response_region` 을 `is_approval(text)` 로 게이트 → 라이브 모달이 아닐 때는 경계 truncation 을 하지 않음.
- 추가로 매칭 위치가 tail 60줄 안에 있어야만 유효 → 오래된 scrollback 의 echo 는 걸러냄.
- **본질**: VT 기반 설계에서는 row index 가 직접 주어지므로 "경계 탐색" 자체가 사라짐 (§2 스코프, _response_region 폐기).

**근거**: bugreport/20260418_094744 (Fix 1 코드), step-3-design.md §2 (VT paradigm 에서 _response_region 폐기).

---

## 4. Alt-Screen 분리 및 Modal Overlay 격리

**현상황**:
- fzf / git log / less 같은 alt-screen tool 이 pane 내용을 겹쳐도 main-screen dedup 로직에 영향 → 도구 UI 가 응답으로 오탐될 수 있음.

**신설계**:
- LineCommitter 가 VT state 의 `in_alt_screen` flag 를 추적 → commit 시점에 기록.
- RegionTagger 가 alt 영역을 `region="modal_overlay"` 로 격리.
- EventClassifier 가 이 region 을 Telegram 에 보내지 않음 (step-3-design.md §6 리스크 표).
- **결과**: alt-screen tool UI 가 응답 스트림을 오염시킬 수 없음.

**근거**: tools/line_commit.py:34 (in_alt field), tools/region_tagger.py (region 분류), step-3-design.md §6 (alt-screen 리스크 완화).

---

## 5. VTScreen 구현으로 row 위치 정확성 보증

**현상황 (capture-pane 기반)**:
- 매 tick 전체 스냅샷을 재파싱 → 같은 row 의 블록이 여러 번 나타나면 dedup 로직이 tail 정책에만 의존 (문제: §3 참조).
- 동시에 ANSI 애니메이션(스피너, 커서 위치) 중에 partial 스냅샷 → "blinking" 구간 감지 불안정.

**신설계**:
- VTScreen: ANSI 토큰을 즉시 반영, row 의 "확정 시점" 을 cursor 위치 + K-token timer 로 판정 (LineCommitter.R1).
- 결과: 같은 위치에서 반복 나타나는 스피너 업데이트 → 1번만 commit.
- step-2-results.md: case-04 에서 `busy_enter 30건 ÷ 450KB ÷ 7분 = 0.07건/초` (기준 5건/초 대비 70배 여유).

**근거**: tools/vt_screen.py (VT 구현), tools/line_commit.py (R1 timer), step-2-results.md §4 (스피너 rate 충족).

---

## 6. 구조적 Testability 및 변경 국소성

**현상황**:
- `parser.py` 의 `_response_region` / `is_approval` / `extract_response_blocks` 가 동일 함수/모듈 안에 tail 정책 + 경계 탐색 + dedup 이 뒤엉킴 (§2 스코프 언급).
- 새 case 회귀가 나면 여러 계층을 동시에 수정해야 함.

**신설계**:
- 각 모듈(Tokenizer → VTScreen → LineCommitter → RegionTagger → EventClassifier)이 단일 책임.
- 입/출력이 dataclass 로 고정 → 테스트 간 경계가 명확 (step-3-design.md §3, §7 Criterion 3a-3c).
- case-05 같은 새 이벤트 타입 추가 시 EventClassifier + 테스트만 touch (변경 국소성).

**근거**: step-3-design.md §7 Criterion 3a/3b/3c (3축 유지보수 면적), step-2-results.md §5 (테스트 격리도 Pass).

---

## 7. Shadow Run 기반 점진적 전환

**현상황 (기존 release 정책)**:
- 새 기능을 "개발" 테스트 후 바로 프로덕션 배포 → 회귀는 즉시 고객이 느낌.
- 지난 3주 동안 3건의 회귀 fix (bc6a015 / 4c20696 / 20260418_094744) → 각각 24시간 후 발견.

**신설계 (Step 4-α)**:
- 분석기를 기존 파서와 **병행 실행** → 별도 파일에 이벤트 diff 기록 (shadow-diff.jsonl).
- 72시간 minimum 관찰 → `block_commit` 개수 ±5% 내, `approval_show` superset 조건.
- **효과**: 프로덕션 cutover 전에 실측 안정성 증명 가능. 회귀는 개발 단계에서 잡힘.

**근거**: step-3-design.md §5 Step 4-α (shadow run 상세), §9 (exit criteria).

---

## 결론

파이프라인 설계의 신뢰성 강점은 **근본 원인 해결** (offset dedup, alt-screen 분리, row 위치 정확성) 에 있다. 기존 아키텍처의 "스냅샷 + tail scan" 구조에서 비롯된 동종 계열 회귀(eviction, boundary, echo 오탐)는 paradigm 전환으로 대부분 제거된다. 단, §실패 모드 섹션에서 다룰 새로운 위험(rotation race, analyzer crash resume, 이벤트 손상)도 있으므로 step 4-α shadow run 이 필수 검증 단계.
