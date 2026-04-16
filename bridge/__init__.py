"""claude-bridge 내부 컴포넌트.

레이어링 (아래에서 위로 단방향):
    config  — 상수/설정/로거/권한 게이트
    tmux    — tmux 원시 조작 (capture-pane, send-keys)
    parser  — pure 함수 (ANSI/상태/⏺ 블록 파싱)
    session — Claude 세션 파일 탐색 + 인스턴스간 락
    sender  — Telegram 방향 outbound (유저에게 전송)
    core    — Bridge 오케스트레이터 + monitor 루프
    receiver — Telegram 방향 inbound (cmd_*, on_message, on_callback)

bot.py 는 이 패키지를 합쳐 Application 을 구성하는 엔트리 포인트.
"""
