# Claude Code — Development Guidelines

## CRITICAL: Before every change

### 1. Skeptical Agent (1순위)
모든 변경 전에 다음 질문을 스스로 수행한다:
- 이 결정을 의심하라 — 내가 가정하는 것이 실제로 맞는가?
- 다른 에이전트들이 놓친 엣지 케이스는 무엇인가?
- 이 접근이 실패할 수 있는 시나리오는?
- "작동할 것 같다"는 확신과 "검증됐다"를 혼동하지 말 것

### 2. Architect Agent (1순위)
모든 변경 전에 다음 관점에서 검토한다:
- 이 변경이 전체 시스템에 어떻게 맞는가?
- 기술 빚을 만들고 있지는 않은가?
- 나쁜 패턴(중복 호출, 비대칭 책임 등)이 도입되는가?
- 확장성 문제가 있는가?

## Release Checklist (배포 전 필수)

1. `bun test` — 모든 unit/integration 테스트 통과
2. `bun run typecheck` — 타입 에러 없음
3. `bun tests/smoke-spawn.ts` — spawn→hello 왕복 확인
4. 기능이 실제로 동작한다는 **증거**를 확인한 후에만 커밋

## Commit Rules

- **커밋은 사용자가 명시적으로 요청할 때만** 실행
- `git push`는 사용자가 명시적으로 요청할 때만 실행
- Claude/Anthropic co-author attribution 절대 금지
- 기능 미검증 상태에서 커밋하지 말 것

## Testing Approach

- 새 기능 추가 시 → 해당 로직의 unit test 먼저 작성
- 외부 의존성(Telegram API, tmux) 없이 테스트 가능한 부분 격리
- dispatcher.ts 오케스트레이션 로직은 observer/slash/registry를 mocking하여 검증
- `bun tests/smoke-spawn.ts`로 실제 claude 바이너리 통합 확인
