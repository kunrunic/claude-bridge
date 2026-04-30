# Networking — 원격 접속 / 동적 IP 환경 가이드

claude-bridge 의 `cb` 명령은 본질적으로 SSH wrapper 다. 즉 "원격 호스트로
어떻게 도달할 것인가" 는 SSH 만의 문제 = 일반 네트워크 문제. dispatcher
머신이 노트북/홈서버처럼 **고정 IP 가 없는 환경**일 때 사용 가능한
방법을 정리한다.

목차
- [상황별 추천](#상황별-추천)
- [옵션 비교](#옵션-비교)
- [옵션별 셋업](#옵션별-셋업)
  - [Tailscale (★ 권장)](#tailscale--권장)
  - [ZeroTier](#zerotier)
  - [Cloudflare Tunnel](#cloudflare-tunnel)
  - [DDNS + 포트포워딩](#ddns--포트포워딩)
  - [VPS reverse SSH tunnel](#vps-reverse-ssh-tunnel)
  - [ngrok / pinggy](#ngrok--pinggy)
- [claude-bridge 통합 — `cb add` 패턴](#claude-bridge-통합--cb-add-패턴)
- [모바일 / 이동 환경 — `mosh`](#모바일--이동-환경--mosh)
- [Sleep / 절전 대응](#sleep--절전-대응)
- [트러블슈팅](#트러블슈팅)

---

## 상황별 추천

| 상황 | 추천 |
|---|---|
| 집 노트북 (동적 IP / CGNAT) → 회사·외출지에서 접속 | **Tailscale** |
| 본인 도메인 / Cloudflare 계정 이미 운영 중 | **Cloudflare Tunnel** |
| VPS / 클라우드 인스턴스 이미 보유 | **VPS reverse SSH** |
| 회선이 공인 IP + 공유기 포트포워딩 가능 | **DDNS + 포트포워딩** |
| 한 번 데모 / 외부에 잠깐만 노출 | **ngrok / pinggy** |

특별한 사정 없으면 **Tailscale** 부터 시작. 셋업이 가장 짧고, NAT/CGNAT
무관하게 동작하며, 100 device 까지 무료다.

---

## 옵션 비교

| 방식 | 동적 IP | NAT/CGNAT | 인증 | 외부 노출 | 셋업 | 비용 |
|---|---|---|---|---|---|---|
| **Tailscale** | 자동 | 자동 | OAuth (Google/GitHub) | 없음 (mesh VPN) | 양쪽 설치만 | 무료 100 device |
| **ZeroTier** | 자동 | 자동 | 가입/승인 | 없음 (mesh VPN) | 양쪽 설치 | 무료 25 device |
| **Cloudflare Tunnel** | 자동 | outbound only | Cloudflare Access OAuth | https + 도메인 | tunnel 설정 | 무료 (도메인 별도) |
| **DDNS + 포트포워딩** | DNS 갱신 | 공유기 설정 (CGNAT 면 X) | SSH 키 | 22 포트 외부 | 중간 | 무료 (도메인 별도) |
| **VPS reverse tunnel** | reverse SSH 유지 | outbound only | SSH 키 | VPS 의 포트 | 직접 구성 | VPS 월 $3~5 |
| **ngrok / pinggy** | 자동 | 자동 | 토큰 | 임시 도메인 | 한 줄 명령 | 무료 한정 / 유료 도메인 |

---

## 옵션별 셋업

### Tailscale (★ 권장)

**개념** — WireGuard 기반 mesh VPN. 양쪽 device 가 control plane 으로 자동
peer 발견, NAT traversal 자동, 양 device 에 영구 100.x.x.x IP 부여.

**셋업**:

1. https://login.tailscale.com 에서 OAuth (Google/GitHub) 가입
2. 양쪽 머신에 설치
   ```bash
   # macOS
   brew install --cask tailscale
   open -a Tailscale          # GUI 로 OAuth
   # 또는 CLI
   sudo tailscale up
   ```
3. admin console 에서 두 가지 켜기
   - **MagicDNS** ON — `ssh laptop` 같이 hostname 만으로 접속 가능
   - **Disable key expiry** (각 device 의 ⋯ 메뉴) — 영구 device 라 90일 만료 끄는 게 합리적
4. hostname 짧게 변경 (선택)
   ```bash
   sudo tailscale set --hostname=home
   # 또는 admin console → Machines → device → Edit machine name
   ```
5. SSH 키 등록
   ```bash
   ssh-copy-id -i ~/.ssh/id_ed25519 user@home
   ```
6. cb 등록
   ```bash
   cb add home --host=home --user=<user> --key=~/.ssh/id_ed25519
   cb home
   ```

`--host=home` 의 `home` 이 MagicDNS 이름. IP 가 변경되든, 네트워크가 이동
하든 영구 고정. 이후 `cb home` 한 줄로 어디서든 접속.

**검증**:
```bash
tailscale status            # device 두 개 다 active 표시되어야
tailscale ping home         # mesh 연결 확인
```

**옵션 — Tailscale SSH** (SSH 키 관리 면제)

`sudo tailscale up --ssh` 양쪽에서 실행하면 Tailscale 가 SSH 인증을 대신.
cb 의 `--key=` 인자는 빼고 등록.

---

### ZeroTier

Tailscale 과 거의 같은 구조의 SDN. 무료 25 device 한정. 운영 회사 / 정책
선호에 따라 선택. claude-bridge 통합은 Tailscale 과 동일 (각 device 가
부여받은 IP 또는 도메인을 cb host 로).

**셋업 요약**:
```bash
brew install --cask zerotier-one  # macOS
sudo zerotier-cli join <network-id>
# admin console (my.zerotier.com) 에서 device 승인
```

ZeroTier 자체는 MagicDNS 같은 자동 hostname resolution 이 없어 직접 DNS
세팅 (예: hostsfile, 또는 ZeroTier Hosted DNS) 가 필요. 그래서 일반적으로
Tailscale 이 셋업 비용이 더 적다.

---

### Cloudflare Tunnel

**개념** — 노트북에서 `cloudflared` 가 outbound 로 Cloudflare 에 터널 유지,
외부 사용자는 본인 도메인의 hostname 으로 접근. SSH 도 가능
(`cloudflared access ssh`).

**전제** — 본인 도메인이 Cloudflare DNS 사용 중이어야 함.

**셋업 요약**:

호스트 (노트북) 측:
```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create my-laptop
cloudflared tunnel route dns my-laptop laptop.example.com

# ~/.cloudflared/config.yml
# tunnel: <tunnel-id>
# credentials-file: /Users/me/.cloudflared/<tunnel-id>.json
# ingress:
#   - hostname: laptop.example.com
#     service: ssh://localhost:22
#   - service: http_status:404

cloudflared tunnel run my-laptop
```

클라이언트 (회사 PC) 측 SSH config:
```
Host laptop
  HostName laptop.example.com
  User me
  ProxyCommand cloudflared access ssh --hostname %h
```

cb 등록:
```bash
cb add laptop --host=laptop --user=me
```

장점: 외부에서 도메인으로 접속 가능, Cloudflare Access 로 OAuth gating
가능. 단점: 도메인 / Cloudflare 계정 / config 파일 셋업이 Tailscale 보다
번거롭다.

---

### DDNS + 포트포워딩

**개념** — 공유기에 SSH 포트 (22 또는 임의) 포트포워딩. 공인 IP 가 변할
때마다 DDNS 서비스가 도메인 → IP 매핑을 갱신.

**전제**:
- ISP 가 **공인 IP** 를 줘야 함. 한국 ISP 일부 (KT 일부 회선, 알뜰모바일
  계열) 는 **CGNAT** 라 공유기에 들어오는 IP 자체가 공인이 아니어서
  포트포워딩 불가. 회선 사양 / ISP 에 확인 필요
- 공유기 관리자 페이지 접근 필요

**셋업 요약**:

1. 공유기 관리자 페이지 → 포트포워딩 추가
   ```
   외부 포트: 22 (또는 임의 — 2222 권장)
   내부 IP: 노트북의 LAN IP
   내부 포트: 22
   ```
2. DDNS 서비스 선택
   - [DuckDNS](https://www.duckdns.org/) — 무료, 토큰 발급
   - [Cloudflare DNS API](https://developers.cloudflare.com/api/) — 본인 도메인 + Cloudflare API
   - 공유기 자체 DDNS (asus DDNS, ipTime DDNS 등) — 가장 간단
3. cron 또는 ddclient 로 IP 갱신
   ```bash
   # 노트북에 cron (DuckDNS 예시)
   */5 * * * * curl -s "https://www.duckdns.org/update?domains=mylaptop&token=XXX&ip="
   ```
4. cb 등록
   ```bash
   cb add laptop --host=mylaptop.duckdns.org --port=2222 --user=me
   ```

장점: 가장 전통적, 외부 의존 적음. 단점: CGNAT 무용, 22 포트 외부 노출,
공유기 변경 필요.

---

### VPS reverse SSH tunnel

**개념** — 외부 VPS 한 대에 노트북이 outbound 로 reverse SSH 터널 유지.
클라이언트는 VPS 의 특정 포트로 SSH 접속하면 노트북으로 forwarding.

**전제** — VPS 한 대 (DigitalOcean, Vultr, Linode, OCI 무료 등). 월
$3~5 또는 무료 tier.

**셋업 요약**:

노트북 (호스트) → systemd / launchd 로 reverse 터널 영속화:
```bash
ssh -fN -R 2222:localhost:22 user@vps.example.com
# 또는 autossh 로 자동 재연결
autossh -fN -M 0 -R 2222:localhost:22 user@vps.example.com
```

클라이언트:
```bash
cb add laptop --host=vps.example.com --port=2222 --user=me
```

장점: 본인이 모든 layer 통제. 단점: VPS 운영 부담, 터널 영속 셋업 필요.

---

### ngrok / pinggy

**개념** — 임시로 외부 URL 받아서 SSH 노출. 데모 / 일회성 / 셋업 시간
없는 즉석 케이스.

**셋업 요약**:
```bash
# ngrok
brew install ngrok
ngrok config add-authtoken <token>
ngrok tcp 22
# → tcp://0.tcp.ngrok.io:12345 같은 endpoint 출력

# pinggy
ssh -p 443 -R0:localhost:22 a.pinggy.io
```

cb:
```bash
cb add laptop --host=0.tcp.ngrok.io --port=12345 --user=me
```

단점: 무료 플랜은 endpoint 가 매 실행 시 변경됨 → cb 항목도 매번 수정 필요.
유료 (월 $5~) 면 reserved domain. 영속 사용엔 다른 옵션이 합리적.

---

## claude-bridge 통합 — `cb add` 패턴

위 모든 옵션의 공통점: **cb 명령 자체는 동일**. 단지 `--host` / `--port`
값이 옵션마다 달라질 뿐.

| 옵션 | `cb add` 형태 |
|---|---|
| Tailscale | `cb add home --host=home --user=me` (MagicDNS 이름) |
| ZeroTier | `cb add home --host=10.x.x.x --user=me` (또는 ZT DNS) |
| Cloudflare Tunnel | `cb add home --host=home.example.com --user=me` (+ `~/.ssh/config` 의 `ProxyCommand`) |
| DDNS | `cb add home --host=mybox.duckdns.org --port=2222 --user=me` |
| VPS reverse | `cb add home --host=vps.example.com --port=2222 --user=me` |
| ngrok | `cb add home --host=0.tcp.ngrok.io --port=12345 --user=me` |

cb 는 등록된 host 를 ssh + tmux attach 흐름으로 호출하므로, 위 어떤
방식이든 **ssh 가 통하면 cb 는 그대로 동작**한다.

---

## 모바일 / 이동 환경 — `mosh`

지하철·카페 이동, Wi-Fi → LTE 전환 같은 **네트워크 변경에 강한 SSH 대체**:

```bash
brew install mosh        # 호스트 + 클라이언트
mosh user@home           # SSH 대신 호출 (UDP 60000-61000 필요)
```

장점:
- 네트워크 끊겨도 자동 복귀, 입력 안 잃음
- Sleep 후 깨움 즉시 재연결
- Local echo — 모바일 키보드 즉시 반응

`tmux` 가 이미 detach 후 살아있으므로 SSH 끊겨도 Claude 자체는 안 죽지만,
이동 환경이면 mosh 가 **사용자 측 재접속 부담**을 없앤다.

cb 는 ssh wrapper 라 mosh 와 직접 통합되진 않지만, `cb home` 으로 한 번
attach 해서 작업 후 detach 한 다음 mosh 로 같은 호스트에 들어가서
`tmux attach -t cb-menu` 하면 같은 메뉴 재진입 가능.

---

## Sleep / 절전 대응

dispatcher 머신이 macOS 노트북이고 lid close 또는 절전 모드면 외부 접속이
끊긴다.

**옵션 1 — 항상 깨어있기**
```bash
sudo pmset -a sleep 0 disksleep 0          # 영구
caffeinate -dimsu                          # 일회성 (foreground)
caffeinate -dimsu -t 28800 &               # 8시간만
```

**옵션 2 — Wake on Network**
- 시스템 설정 → 배터리 / 어댑터 → "네트워크 접근 시 깨어남" ON
- 단, lid 가 닫혀있으면 일부 macOS 버전에서 sleep 진입 자체를 막지 못함

**옵션 3 — Lid close 무시**
- macOS 자체엔 옵션 없음. 외부 디스플레이 + 전원 + 외부 키보드 연결로 우회.
- 또는 `Amphetamine`, `InsomniaX` 같은 서드파티 앱.

집 노트북을 dispatcher 로 운영하려면 보통 **옵션 1 (전원 연결 + sleep 0)
+ lid open** 조합이 가장 안정적.

---

## 트러블슈팅

### `tailscale ping <host>` 가 `relay` 만 뜨고 direct 안 됨
NAT traversal 실패. 동작은 하지만 latency 더 높음. 공유기에서 UPnP /
NAT-PMP 켜면 direct 잡힐 가능성. 일반 사용엔 relay 도 충분.

### `ssh home` 이 `Connection refused`
- 노트북 sshd 미가동: 시스템 설정 → 일반 → 공유 → **원격 로그인** ON
- 또는 ssh 데몬 죽음: `sudo launchctl kickstart -k system/com.openssh.sshd`

### `ssh home` resolve 실패
- Tailscale MagicDNS 미활성: admin console DNS 탭에서 토글 ON
- 또는 macOS DNS 캐시: `sudo dscacheutil -flushcache`

### CGNAT 환경에서 DDNS / 포트포워딩 무용
ISP 한테 공인 IP 부여 요청 (가능한 경우 신청 필요). 불가능하면 Tailscale
또는 VPS reverse 같은 outbound-only 방식으로 전환.

### Tailscale device key 가 만료돼서 갑자기 끊김
admin console → Machines → device → ⋯ → **Disable key expiry** 권장
(영구 device 의 경우).

### dispatcher 사라짐 / cb-menu 부재
호스트가 sleep 진입 또는 dispatcher 가 crash. 호스트에 다시 진입해서
`./bin/start.sh` 또는 `tmux ls` 로 상태 확인.

---

## 참고

- Tailscale 공식 문서: https://tailscale.com/kb/
- Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
- mosh: https://mosh.org/
- DuckDNS: https://www.duckdns.org/
