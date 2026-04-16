"""Claude 세션 파일 검색 + 인스턴스간 락.

락 파일 형식: `~/.claude/.cb_lock_<session_id>`
  line 1: 소유 tmux 세션 이름
  line 2: 소유 chat_id
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import config, tmux
from .config import _log

_LOCK_DIR = Path.home() / ".claude"


# -- 락 ----------------------------------------------------------------------

def _lock_path(session_id: str) -> Path:
    return _LOCK_DIR / f".cb_lock_{session_id}"


def _parse_lock(session_id: str) -> tuple[str, int] | None:
    """락파일에서 (tmux_name, chat_id) 반환. 없거나 파싱 실패 시 None."""
    lp = _lock_path(session_id)
    try:
        lines = lp.read_text().splitlines()
        return lines[0].strip(), int(lines[1].strip())
    except Exception:
        return None


def _acquire_lock(session_id: str, chat_id: int) -> bool:
    """락 획득. 이미 다른 인스턴스가 점유 중이면 False."""
    lp = _lock_path(session_id)
    try:
        fd = lp.open("x")
        fd.write(f"{config.TMUX}\n{chat_id}")
        fd.close()
        return True
    except FileExistsError:
        info = _parse_lock(session_id)
        if info:
            owner_tmux, _ = info
            if tmux.tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
                lp.unlink(missing_ok=True)
                return _acquire_lock(session_id, chat_id)
        return False
    except Exception as e:
        _log("LOCK-ERROR", str(e))
        return True  # 락 디렉토리 문제 시 허용 (방어적)


def _release_lock(session_id: str | None):
    """내 TMUX 세션이 소유한 락만 삭제."""
    if not session_id:
        return
    lp = _lock_path(session_id)
    try:
        info = _parse_lock(session_id)
        if info and info[0] == config.TMUX:
            lp.unlink(missing_ok=True)
    except Exception as e:
        _log("LOCK-RELEASE-ERROR", str(e))


def _is_locked(session_id: str) -> bool:
    lp = _lock_path(session_id)
    if not lp.exists():
        return False
    info = _parse_lock(session_id)
    if not info:
        lp.unlink(missing_ok=True)
        return False
    owner_tmux, _ = info
    if tmux.tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
        lp.unlink(missing_ok=True)
        return False
    return True


def _my_locks() -> list[tuple[str, int]]:
    """이 인스턴스(TMUX)가 소유한 락 목록 → [(session_id, chat_id), ...]."""
    result = []
    try:
        for lp in _LOCK_DIR.glob(".cb_lock_*"):
            session_id = lp.name[len(".cb_lock_"):]
            info = _parse_lock(session_id)
            if info and info[0] == config.TMUX:
                result.append((session_id, info[1]))
    except Exception as e:
        _log("MY-LOCKS-ERROR", str(e))
    return result


# -- 세션 탐색 ---------------------------------------------------------------

def find_sessions(limit: int = 8) -> list[dict]:
    now = time.time()
    candidates = []
    for p in config.PROJECTS.rglob("*.jsonl"):
        if p.stem.startswith("agent-"):
            continue
        title, last, last_ts = _parse_session_msgs(p)
        if not title:
            continue
        activity_ts = last_ts if last_ts > 0 else p.stat().st_mtime
        if _is_locked(p.stem):
            continue
        proj_slug = p.parent.name.lstrip("-")
        proj_short = proj_slug.split("-")[-1] if proj_slug else ""
        candidates.append({
            "id":          p.stem,
            "project":     proj_short,
            "activity_ts": activity_ts,
            "mtime":       datetime.fromtimestamp(activity_ts).strftime("%m/%d %H:%M"),
            "title":       title[:40],
            "last":        last[:40],
        })

    candidates.sort(key=lambda x: x["activity_ts"], reverse=True)
    return candidates[:limit]


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item["text"]
    return ""


def _parse_session_msgs(path: Path) -> tuple[str, str, float]:
    """(첫 메시지, 마지막 메시지, 마지막 활동 timestamp) 반환."""
    first = ""
    last = ""
    last_ts = 0.0
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    ts_str = d.get("timestamp", "")
                    if ts_str:
                        try:
                            from datetime import datetime as _dt
                            ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                            if ts > last_ts:
                                last_ts = ts
                        except Exception:
                            pass

                    if d.get("type") != "user":
                        continue
                    text = _extract_text(d.get("message", {}).get("content", ""))
                    if not text or text.startswith("<") or len(text) < 15:
                        continue
                    stripped = text.strip()
                    if stripped.startswith("/") and " " not in stripped:
                        continue
                    if not first:
                        first = stripped
                    last = stripped
                except Exception:
                    pass
    except Exception:
        pass
    return first, last, last_ts


def get_session_cwd(session_id: str) -> str | None:
    """JSONL 에서 세션의 cwd 추출."""
    for p in config.PROJECTS.rglob(f"{session_id}.jsonl"):
        try:
            with open(p, errors="ignore") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        if "cwd" in d:
                            return d["cwd"]
                    except Exception:
                        pass
        except Exception:
            pass
    return None
