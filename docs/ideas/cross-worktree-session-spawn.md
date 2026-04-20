# 아이디어: 다른 워크트리에서 Claude Code 세션 원격 생성 (브릿지 이어하기 연동)

> 작성일: 2026-04-16
> 맥락: 텔레그램 봇(claude-bridge2)에서 다른 프로젝트(`ai_env` 워크트리)의 작업을 이어가야 하는데, 봇 사용자는 직접 터미널을 쓸 수 없음

---

## 1. 문제

- 브릿지(`claude-bridge2`) 세션은 cwd 가 브릿지 폴더에 고정됨
- 실제 작업은 다른 경로(`/Users/kunrunic/ai_env/.claude/worktrees/happy-blackwell`)에서 진행 중
- 사용자는 텔레그램 챗봇으로만 접근 → `cd` 후 `claude` 를 직접 실행할 방법이 없음
- 메모리도 프로젝트별(cwd 슬러그별)로 분리되어 있어 엉뚱한 곳에 쌓이면 다음 세션에서 자동 로드 안 됨

## 2. 아이디어 한 줄 요약

**현재 세션의 Bash 에서 `claude --print` 를 다른 cwd 로 호출해 "빈 세션"을 만들어두면, 그 세션이 브릿지의 /resume 목록에 올라오고 사용자가 챗에서 선택만 하면 해당 워크트리로 이어갈 수 있다.**

## 3. 핵심 메커니즘

Claude Code 세션 파일은 cwd 경로를 슬러그로 변환한 디렉터리에 저장됨:

```
~/.claude/projects/<cwd-slug>/<session-id>.jsonl
```

예: cwd 가 `/Users/kunrunic/ai_env/.claude/worktrees/happy-blackwell` 이면

```
~/.claude/projects/-Users-kunrunic-ai-env--claude-worktrees-happy-blackwell/<uuid>.jsonl
```

브릿지의 이어하기 목록은 이 디렉터리 구조를 읽으므로, **어떤 방식이든 해당 경로에 `.jsonl` 을 만들면 목록에 노출**된다.

## 4. 스크립트 템플릿

```bash
#!/bin/bash
# LLM Wiki Phase 2 세션을 happy-blackwell 워크트리에서 생성
set -e

WORKTREE="/Users/kunrunic/ai_env/.claude/worktrees/happy-blackwell"
LOG="/tmp/llm_wiki_session_init.log"

cd "$WORKTREE"

SESSION_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
echo "Session ID: $SESSION_ID"

claude --print \
  --session-id "$SESSION_ID" \
  --name "LLM Wiki 이어서 · Phase 2 §8.5 진입" \
  "세션 초기화. 이 턴에서는 'READY'라고만 답하세요. 다음 턴부터 mcp/DESIGN.md §8 을 읽고 Phase 2 §8.5(구현 단계)부터 진행합니다." \
  > "$LOG" 2>&1 || true

cat "$LOG"
ls -la ~/.claude/projects/ | grep happy-blackwell
```

## 5. 옵션 설명 (중요한 것만)

| 옵션 | 의미 | 왜 필요한가 |
|---|---|---|
| `--print` | 비대화식 1턴 실행 후 종료 | 챗봇 안에서 claude 를 띄울 때 인터랙티브 프롬프트를 만들 수 없음 |
| `--session-id <uuid>` | 세션 ID 고정 | 생성 직후 어떤 파일이 만들어졌는지 알 수 있음. 추적/삭제 용이 |
| `--name "<title>"` | /resume 목록 표시 제목 | 브릿지 이어하기 화면에서 한눈에 식별 가능. **이게 없으면 첫 메시지 한 줄을 자동 요약** |
| `--permission-mode` | 권한 모드 | 기본(`default`) 으로 충분. 필요 시 `bypassPermissions` |
| `--no-session-persistence` | 저장 안 함 | 이 아이디어에서는 **쓰지 말 것** (저장돼야 이어하기 가능) |

## 6. 첫 프롬프트 설계 팁

- **짧게**: 토큰 낭비 최소화. `"'READY'라고만 답하세요"` 가 무난
- **다음 턴 지시 포함**: 사용자가 세션을 열었을 때 "다음에 뭘 할지" 가 대화 기록 안에 남아 있어야 자연스럽게 이어짐
- **핵심 파일 경로 명시**: 예) `mcp/DESIGN.md §8.5` — 메모리 자동 로드와는 별개로 대화 로그에 위치가 박혀 있으면 복구가 안정적

## 7. 주의사항

1. **cwd 슬러그 규칙 확인** — 경로의 `/` 가 `-` 으로, `.` 도 `-` 으로 치환되며 일부 문자는 연속 `-` 로 합쳐지지 않음. 실제 생성된 디렉터리명을 `ls ~/.claude/projects/` 로 반드시 확인
2. **API 토큰 소모** — `--print` 는 실제 1턴 호출. 무료 아님. 짧은 프롬프트로 최소화
3. **권한** — `--permission-mode default` 에서도 파일 수정은 안 하므로 문제없지만, 스크립트 내 명령이 편집/삭제를 포함하면 사용자 승인 없이 돌지 않도록 조심
4. **임시 스크립트는 `/tmp` 에** — 저장소에 커밋되지 않도록. 필요하면 아이디어 폴더에만 보존
5. **메모리 저장 위치** — 세션이 새 cwd 에서 열리므로 그 세션의 메모리도 **새 프로젝트 슬러그 경로**에 저장됨. 기존 메모리를 참조하려면 해당 경로에 미리 `project_*.md` 가 있어야 함

## 8. 확장 아이디어

- **브릿지에 슬래시 커맨드로 편입**: `/spawn <worktree-path> "<title>" "<first-prompt>"` 같은 명령을 bot.py 에 추가하면 챗에서 바로 새 세션 스폰 가능
- **세션 템플릿화**: 자주 쓰는 프로젝트(예: ai_env / llm-wiki / uxcutor …) 별로 프리셋 스크립트를 `아이디어/` 또는 `scripts/` 에 모아두기
- **세션 메타데이터 주입**: 첫 프롬프트에 메모리 경로·진입점·참고 문서를 JSON 으로 박아두고, 이어받은 세션이 그것부터 파싱하게 하면 복구 안정성↑
- **자동 정리**: 더미 스폰 세션이 누적되지 않도록, `.jsonl` 크기가 작고 오래된 것은 주기적으로 정리 (단, /resume 기록은 사용자 자산이므로 삭제는 항상 확인)

## 9. 이번에 만든 실물

- **스크립트**: `/tmp/start_llm_wiki_session.sh` (임시)
- **생성된 세션 ID**: `4e77a79f-918a-455b-a35c-b898d8470e44`
- **저장 경로**: `~/.claude/projects/-Users-kunrunic-ai-env--claude-worktrees-happy-blackwell/4e77a79f-918a-455b-a35c-b898d8470e44.jsonl`
- **제목(브릿지 표시)**: `LLM Wiki 이어서 · Phase 2 §8.5 진입`
