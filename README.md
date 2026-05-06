# claude-bridge

**내 PC 의 Claude Code 를, 어디서든.**

집 노트북에 띄워둔 세션을 외출 중에도 폰으로 이어간다.
지하철에서 떠오른 수정도, 자기 전 침대에서의 PR 리뷰도 — 채팅 한 줄로.

---

## 이런 순간을 위해

- 💡 떠오른 수정을 노트북 켜기 전에 미리 시켜두고 싶을 때
- 🌙 긴 빌드·배포는 호스트에 맡기고, 진행상황만 폰으로 따라가고 싶을 때
- 🔀 리서치 / 구현 / 리뷰 세션을 따로 띄워놓고 한 채팅창에서 오가고 싶을 때
- 🔐 원격이지만 도구 호출 하나하나는 직접 승인하고 싶을 때

---

## 30초 시작

[mdwiz](https://github.com/kunrunic/mdwiz) 가 의존성 확인부터 봇 등록까지 자동으로 처리한다.

```bash
# mdwiz 설치 (최초 1회)
git clone https://github.com/kunrunic/mdwiz.git
bash mdwiz/setup.sh

# claude-bridge 설치 + 셋업
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge && mdwiz
```

설치 자세히: **[Getting Started](docs/getting-started.md)**

---

## 두 가지 사용 방식

| | Telegram bot | cb CLI |
|---|---|---|
| **어디서** | 폰, 어디든 메신저 되는 곳 | 노트북·iPad 터미널 |
| **모습** | 채팅창에 메시지 · Allow/Deny 버튼 · 진행 이모지 | `cb home` 한 번에 호스트의 native Claude TUI |
| **장점** | 키보드 없이도 됨, 푸시 알림 | 0ms 지연, vim·scrollback·복붙 그대로 |

둘 다 동시에 써도 된다. 같은 세션을 양쪽에서 봐도 되고, 채널별로 다른 세션을 운영해도 된다.

---

## 더 알아보기

| 문서 | 내용 |
|---|---|
| **[Getting Started](docs/getting-started.md)** | 의존성, 두 가지 셋업 모드, 첫 세션 만들기 |
| **[Telegram 사용](docs/telegram.md)** | 명령어, 라이브 피드백, 권한 승인, 첨부 파일 |
| **[cb CLI 사용](docs/cli.md)** | 호스트 등록·접속, `cb-menu` 키, F-key 단축키 |
| **[네트워크 설정](docs/networking.md)** | Tailscale, Cloudflare Tunnel, 동적 IP, mosh |
| **[운영](docs/operations.md)** | 백그라운드 실행, 멀티 인스턴스, 재기동·복구 |
| **[문제 해결](docs/troubleshooting.md)** | bugreporter / capture / repairer |
| **[설계 문서](docs/design/README.md)** | 아키텍처, MCP 프로토콜, 세션 라이프사이클 |

---

## 요구사항

| | 비고 |
|---|---|
| macOS / Linux 호스트 | claude-bridge 가 실행되는 머신 |
| [Bun](https://bun.sh) 1.2+ | 런타임 |
| tmux | 세션 관리 |
| Claude Code CLI | `claude` 명령이 PATH 에 |
| Telegram Bot Token | Telegram 모드만 — [@BotFather](https://t.me/botfather) |

---

## License

MIT — [LICENSE](LICENSE).

MCP 채널 프로토콜은 [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official) 의 Telegram 플러그인을 레퍼런스로 삼아 독립 구현했다.
