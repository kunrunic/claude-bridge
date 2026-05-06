# 네트워크 설정

`cb` 명령은 본질적으로 SSH wrapper 다 — 호스트로 SSH 가 통하면 cb 도 동작한다.
이 문서는 **Tailscale** 로 home 머신에 SSH 가 통하게 만드는 법만 다룬다.

> 다른 네트워크 도구(ZeroTier, Cloudflare Tunnel, DDNS, ngrok 등) 도 SSH 만 통하면 cb 와 함께 쓸 수 있지만, claude-bridge / mdwiz 는 Tailscale 만 자동 셋업·테스트한다.

---

## 왜 Tailscale 인가

WireGuard 기반 mesh VPN. 양쪽 device 가 control plane 을 통해 자동 peer 발견,
NAT/CGNAT 무관, 양 device 에 영구 100.x.x.x IP 부여, 100 device 까지 무료.

home 노트북이 동적 IP 든, 카페 Wi-Fi 든, LTE 든 — 한 번 셋업하면 `cb home` 한 줄로 어디서든 닿는다.

---

## mdwiz 로 자동 셋업

[Getting Started](getting-started.md) 의 mdwiz 흐름이 다음을 자동 처리한다:

| 단계 | mdwiz 가 자동 |
|---|---|
| Tailscale 설치 | `brew install --cask tailscale` (macOS) / `curl ... \| sh` (Linux) |
| 인증 트리거 | `sudo tailscale up` (브라우저 OAuth 발생) |
| hostname 설정 | `sudo tailscale set --hostname=home` (또는 `office`) |
| 연결 확인 | `tailscale ping home` |
| SSH 키 생성·등록 | `ssh-keygen` + `ssh-copy-id` |
| `cb add home` | 호스트 등록 + `~/.ssh/config` 동기화 |

**사용자가 직접 해야 하는 것** (자동화 불가 영역):

1. **Tailscale 계정 가입** — 첫 `sudo tailscale up` 시 브라우저가 열림 → Google/GitHub OAuth
2. **admin console 토글** — https://login.tailscale.com/admin
   - **DNS** 탭 → **MagicDNS ON** — `ssh home` 같이 hostname 으로 접속 가능
   - **Machines** 탭 → device 의 ⋯ → **Disable key expiry** — 영구 device 라 90일 만료 끄는 게 합리적
3. **macOS 원격 로그인 ON** (home 머신만) — 시스템 설정 → 일반 → 공유 → **원격 로그인**

이 셋만 손으로 처리하면 된다.

---

## 수동 셋업

mdwiz 없이 직접 진행하는 경로. 자동화에 의존하지 않는 환경, 또는 단계별로 무엇이 일어나는지 보고 싶을 때.

```bash
# 1. 양쪽 머신에 설치
# macOS
brew install --cask tailscale
# Linux
curl -fsSL https://tailscale.com/install.sh | sh

# 2. 인증 + 연결 (브라우저 OAuth 발생)
sudo tailscale up

# 3. 짧은 hostname 설정
sudo tailscale set --hostname=home          # home 머신에서
sudo tailscale set --hostname=office        # office 머신에서

# 4. (admin console) MagicDNS ON + Disable key expiry — 위 "사용자가 직접" 참고

# 5. SSH 키 생성 (없으면, office 머신에서)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''

# 6. 공개키를 home 에 등록 (office 에서)
ssh-copy-id -i ~/.ssh/id_ed25519.pub <home-user>@home

# 7. cb 등록 (office 에서)
cb add home --host=home --user=<home-user> --key=~/.ssh/id_ed25519
cb home
```

`--host=home` 의 `home` 이 MagicDNS 이름. 두 device 의 IP 가 변하든, 네트워크가 이동하든 영구 고정.

---

## 검증

```bash
tailscale status            # 두 device 다 "active" 표시되어야
tailscale ping home         # mesh 연결 확인 (latency 수치 출력)
ssh home 'echo ok'          # SSH 통과 확인
```

`tailscale ping home` 이 `relay` 만 뜨고 direct 연결이 안 잡힌다면 NAT traversal 실패.
동작은 하지만 latency 가 더 높음. 공유기에서 UPnP / NAT-PMP 켜면 direct 잡힐 가능성 높아진다.
일반 사용엔 relay 도 충분.

---

## 옵션 — Tailscale SSH (키 관리 면제)

`sudo tailscale up --ssh` 를 양쪽 머신에서 실행하면 Tailscale 가 SSH 인증을 대신 처리한다.

```bash
# home 과 office 양쪽에서
sudo tailscale up --ssh

# cb 등록 시 --key 빼고
cb add home --host=home --user=<home-user>
```

`~/.ssh/authorized_keys` 와 `ssh-copy-id` 단계가 사라진다. ACL 도 admin console 에서 설정.

---

## 모바일 / iPad SSH — mosh

지하철·카페 이동, Wi-Fi → LTE 전환 같은 네트워크 변경에 강한 SSH 대체.
**iPad 나 태블릿에서 터미널 앱으로 SSH 접속할 때만** 의미 있다.

일반 모바일 사용 (Telegram) 에는 불필요.
데스크톱 SSH 도 유선/Wi-Fi 가 안정적이라 불필요.

iPad 에서 SSH 를 쓴다면:

- **호스트**: `brew install mosh` + 라우터에서 UDP 60000–61000 포트 개방
- **클라이언트 앱**: [Blink Shell](https://blink.sh) (iOS, 유료 — mosh 내장된 주요 iOS 앱)

```
Blink Shell (iPad) ──mosh──▶ home ──tmux──▶ Claude TUI
```

cb 자체는 ssh wrapper 라 mosh 와 직접 통합되진 않지만, Blink Shell 에서 mosh 로 접속한 뒤 `tmux attach -t cb-menu` 하면 cb-menu 재진입 가능.

mosh 는 **mdwiz / setup.sh 에 포함되지 않으며** 다른 사용 시나리오에서는 불필요하다.

---

## Sleep / 절전 대응

claude-bridge 호스트가 macOS 노트북이고 lid close 또는 절전 모드면 외부 접속이 끊긴다.
폰으로 메시지 보내도 응답 안 함.

**옵션 1 — 항상 깨어있기**
```bash
sudo pmset -a sleep 0 disksleep 0          # 영구
caffeinate -dimsu                          # 일회성 (foreground)
caffeinate -dimsu -t 28800 &               # 8시간만
```

**옵션 2 — Wake on Network**
시스템 설정 → 배터리 / 어댑터 → "네트워크 접근 시 깨어남" ON.
단, lid 가 닫혀있으면 일부 macOS 버전에서 sleep 진입 자체를 막지 못함.

**옵션 3 — Lid close 무시**
macOS 자체엔 옵션 없음. 외부 디스플레이 + 전원 + 외부 키보드 연결로 우회.
또는 `Amphetamine` 같은 서드파티 앱.

집 노트북을 claude-bridge 호스트로 운영하려면 보통 **옵션 1 (전원 연결 + sleep 0) + lid open** 이 가장 안정적이다.

---

## 트러블슈팅

### `ssh home` 이 `Connection refused`

- home 머신 sshd 미가동: 시스템 설정 → 일반 → 공유 → **원격 로그인** ON
- 또는 ssh 데몬 죽음: `sudo launchctl kickstart -k system/com.openssh.sshd`

### `ssh home` resolve 실패 (`Could not resolve hostname`)

- Tailscale MagicDNS 미활성: admin console → DNS 탭 → MagicDNS 토글 ON
- macOS DNS 캐시: `sudo dscacheutil -flushcache`

### Tailscale device key 가 만료돼서 갑자기 끊김

admin console → Machines → device → ⋯ → **Disable key expiry** 권장
(영구 device 의 경우).

### claude-bridge 사라짐 / cb-menu 부재

호스트가 sleep 진입 또는 claude-bridge 가 crash.
`cb start home` 으로 원격 재기동, 또는 호스트에 직접 접근해서 `./bin/start.sh`.

진단 도구 자세히 → **[문제 해결](troubleshooting.md)**

---

## 참고

- Tailscale 공식 문서: https://tailscale.com/kb/
- mosh: https://mosh.org/
