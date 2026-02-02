import asyncio
import os
import time
import logging
import re
import yaml

from wyoming.event import async_read_event, async_write_event
from wyoming.asr import Transcript

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("wyoming-stt-proxy")

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "10301"))

UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "host.docker.internal")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "10300"))

RULES_FILE = os.getenv("RULES_FILE", "/app/rules.yaml")

MIN_REQUEST_INTERVAL_MS = int(os.getenv("MIN_REQUEST_INTERVAL_MS", "0"))  # 0이면 비활성

# 연결별(세션별) 보호용 - 너무 빨리 재요청하면 upstream이 깨질 수 있어 간단한 쓰로틀
_last_request_ms = 0


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_basic(text: str) -> str:
    """기본 정리: 공백/구두점 정리"""
    t = (text or "").strip()
    # 구두점 -> 공백
    t = re.sub(r"[,\.\?\!]+", " ", t)
    # 공백 정리
    t = re.sub(r"\s+", " ", t).strip()
    return t

def normalize_compact(text: str) -> str:
    # 기본 정규화
    t = normalize_basic(text)
    # 공백/기호 제거: 한글/영문/숫자만 남김
    t = re.sub(r"[^0-9a-zA-Z가-힣]", "", t)
    # 소문자 통일
    return t.lower()


class RuleEngine:
    def __init__(self, rules_path: str):
        self.rules_path = rules_path
        self.rules = []
        self._mtime: float = 0.0   # ✅ 추가
        self.load(force=True)

    # def load(self):
    def load(self, force: bool = False) -> None:
        """rules.yaml을 읽어서 self.rules에 반영"""
        try:
            mtime = os.path.getmtime(self.rules_path)
        except FileNotFoundError:
            if force:
                log.error("Rules file not found: %s", self.rules_path)
                self.rules = []
            return

        if (not force) and (mtime == self._mtime):
            return  # ✅ 변경 없으면 아무 것도 안 함

        try:
            with open(self.rules_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self.rules = data.get("rules", []) or []
            self._mtime = mtime
            log.info("Rules reloaded: %d rules from %s", len(self.rules), self.rules_path)
        except Exception as e:
            log.error("Failed to load rules from %s: %s", self.rules_path, e)
            # 실패 시 기존 rules 유지하고 싶으면 아래 줄은 주석 처리해도 됨
            # self.rules = []

        # try:
        #     with open(self.rules_path, "r", encoding="utf-8") as f:
        #         data = yaml.safe_load(f) or {}
        #     self.rules = data.get("rules", []) or []
        #     log.info("Loaded %d rules from %s", len(self.rules), self.rules_path)
        # except Exception as e:
        #     log.error("Failed to load rules from %s: %s", self.rules_path, e)
        #     self.rules = []

    def reload_if_changed(self) -> None:
        """매 요청마다 호출해도 부담 거의 없는 hot-reload"""
        self.load(force=False)

    def apply(self, text: str) -> str:
        t0 = normalize_basic(text)

        # 비교용: 공백/기호 제거한 버전
        compact = normalize_compact(text)

        for r in self.rules:
            any_list = r.get("any") or []
            out = r.get("set")
            if not out:
                continue

            for needle in any_list:
                n0 = normalize_basic(str(needle))
                ncompact = normalize_compact(str(needle))

                # 1) 컴팩트 포함 (전등꺼 / 전 등 꺼 / 전, 들, 고 / 전.. 켜? 등 흡수)
                if ncompact and ncompact in compact:
                    return out

                # 2) 원문 포함 (혹시라도 원문 기반 룰 쓰고 있으면 유지)
                if n0 and n0 in t0:
                    return out

        return t0

    # def apply(self, text: str) -> str:
    #     t0 = normalize_basic(text)

    #     # 비교용으로 공백/소문자/기호 조금 더 정리한 버전도 준비
    #     # compact = re.sub(r"\s+", "", t0).lower()
    #     compact = normalize_compact(text)

    #     for r in self.rules:
    #         any_list = r.get("any") or []
    #         out = r.get("set")
    #         if not out:
    #             continue

    #         for needle in any_list:
    #             n0 = normalize_basic(str(needle))
    #             ncompact = re.sub(r"\s+", "", n0).lower()

    #             # 1) 컴팩트 일치 (전등꺼 / 전 등 꺼 / 전,들,고 등 변형 흡수)
    #             if ncompact and ncompact in compact:
    #                 return out

    #             # 2) 원문 포함
    #             if n0 and n0 in t0:
    #                 return out

    #     return t0


engine = RuleEngine(RULES_FILE)


async def pipe(reader, writer, direction: str):
    """Wyoming 이벤트를 읽어서 그대로 write. transcript만 가로채서 바꿔치기."""
    global _last_request_ms

    while True:
        event = await async_read_event(reader)
        if event is None:
            return

        # 업스트림 -> 클라이언트(HA) 방향에서 transcript만 가로채기
        if direction == "upstream_to_client" and event.type == "transcript":
            try:
                tr = Transcript.from_event(event)
                original = tr.text or ""
            except Exception:
                # 버전/포맷 차이 나면 그냥 통과
                original = None

            if original is not None:
                engine.reload_if_changed()
                fixed = engine.apply(original)

                # 너무 빠른 연속요청 완화(선택)
                if MIN_REQUEST_INTERVAL_MS > 0:
                    now = now_ms()
                    if now - _last_request_ms < MIN_REQUEST_INTERVAL_MS:
                        # 너무 촘촘하면 transcript는 그대로 두고 통과
                        pass
                    else:
                        _last_request_ms = now

                if fixed != normalize_basic(original):
                    log.info("Transcript rewrite: '%s' -> '%s'", original, fixed)
                    event = Transcript(text=fixed).event()
                else:
                    # 기본정리만 반영(예: 앞 공백 제거 같은 것)
                    if fixed != original:
                        log.info("Transcript normalize: '%s' -> '%s'", original, fixed)
                        event = Transcript(text=fixed).event()

        await async_write_event(event, writer)
        await writer.drain()


async def handle_client(client_reader, client_writer):
    peer = client_writer.get_extra_info("peername")
    log.info("Client connected: %s", peer)

    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    except Exception as e:
        log.error("Upstream connect failed %s:%d - %s", UPSTREAM_HOST, UPSTREAM_PORT, e)
        client_writer.close()
        await client_writer.wait_closed()
        return

    try:
        t1 = asyncio.create_task(pipe(client_reader, upstream_writer, "client_to_upstream"))
        t2 = asyncio.create_task(pipe(upstream_reader, client_writer, "upstream_to_client"))

        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
    finally:
        try:
            upstream_writer.close()
            await upstream_writer.wait_closed()
        except Exception:
            pass
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass
        log.info("Client disconnected: %s", peer)


async def main():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("Listening on %s; upstream=%s:%d; rules=%s", addrs, UPSTREAM_HOST, UPSTREAM_PORT, RULES_FILE)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

