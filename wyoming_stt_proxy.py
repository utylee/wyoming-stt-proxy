#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# from wyoming.event import Event
# from wyoming.info import Info
# from wyoming.server import AsyncServer
# from wyoming.event import async_read_event, async_write_event
from wyoming.event import Event, async_read_event, async_write_event
# from wyoming.util import async_read_event, async_write_event

log = logging.getLogger("wyoming-stt-proxy")

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "10301"))

UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "wyoming-whisper")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "10300"))

RULES_FILE = os.getenv("RULES_FILE", "/app/rules.yaml")

# Turn handling policy
TRANSCRIPT_TIMEOUT_SEC = float(os.getenv("TRANSCRIPT_TIMEOUT_SEC", "120"))

# ------------------------------------------------------------
# Normalization + Rules
# ------------------------------------------------------------

_RE_MULTI_SPACE = re.compile(r"\s+")
# Keep only letters/digits (Korean, Latin, numbers). Remove punctuation/spaces.
_RE_COMPACT_KEEP = re.compile(r"[^0-9A-Za-z가-힣]+")


def normalize_basic(text: str) -> str:
    """Light normalization: unify some punctuation to spaces, trim."""
    if not text:
        return ""
    t = text
    # replace common punctuation to spaces
    t = re.sub(r"[\"'`]", "", t)
    t = re.sub(r"[\?\!\.\,\:\;\(\)\[\]\{\}\<\>\~\^\|\+\=\-_/\\]", " ", t)
    t = _RE_MULTI_SPACE.sub(" ", t).strip()
    return t


def normalize_compact(text: str) -> str:
    """Strong normalization: remove spaces & punctuation; keep only letters/digits."""
    if not text:
        return ""
    t = normalize_basic(text)
    t = _RE_COMPACT_KEEP.sub("", t)
    return t.lower()


@dataclass
class Rule:
    any: List[str]
    set: str


class RuleEngine:
    """rules.yaml loader with mtime cache + compact substring matching."""

    def __init__(self, rules_path: str):
        self.rules_path = rules_path
        self.mtime: float = 0.0
        self.rules: List[Rule] = []
        self.load(force=True)

    def load(self, force: bool = False) -> None:
        try:
            st = os.stat(self.rules_path)
            new_mtime = st.st_mtime
        except FileNotFoundError:
            # If file missing, keep empty rules but log loudly on force
            if force:
                log.error("Rules file not found: %s", self.rules_path)
            self.rules = []
            self.mtime = 0.0
            return

        if (not force) and (new_mtime == self.mtime):
            return

        try:
            import yaml  # PyYAML inside container

            data = yaml.safe_load(open(self.rules_path, "r", encoding="utf-8")) or {}
            raw_rules = data.get("rules") or []
            parsed: List[Rule] = []
            for r in raw_rules:
                any_list = r.get("any") or []
                out = r.get("set") or ""
                # normalize stored needles too (compact)
                any_list = [str(x) for x in any_list if str(x).strip()]
                out = str(out)
                if any_list and out:
                    parsed.append(Rule(any=any_list, set=out))
            self.rules = parsed
            self.mtime = new_mtime
            log.info("Rules reloaded: %d rules from %s", len(self.rules), self.rules_path)
        except Exception as e:
            log.error("Failed to load rules from %s: %s", self.rules_path, e)
            # keep previous rules if load fails

    def apply(self, text: str) -> str:
        """Rewrite transcript text based on rules.
        Matching strategy:
          - build compact of input
          - for each needle in rule.any:
              needle_compact in input_compact  (substring)
              OR exact match after basic normalization
        """
        self.load(force=False)

        t0 = normalize_basic(text)
        compact = normalize_compact(t0)

        for r in self.rules:
            for needle in r.any:
                n0 = normalize_basic(needle)
                ncompact = normalize_compact(n0)
                if not ncompact:
                    continue
                # substring match is key (robust to spacing/punctuation)
                if (ncompact in compact) or (n0 == t0):
                    return r.set

        return t0


# ------------------------------------------------------------
# Transcript parsing helper (works across wyoming event payload styles)
# ------------------------------------------------------------
def extract_transcript_text(ev: Event) -> Optional[str]:
    """Try to pull transcript text from Event in a tolerant way."""
    try:
        d = ev.data or {}
        # common forms
        if isinstance(d, dict):
            t = d.get("text")
            if isinstance(t, str):
                return t
        # sometimes data may be a stringified dict; try best-effort
        if isinstance(d, str):
            # naive JSON-ish extraction
            m = re.search(r'"text"\s*:\s*"([^"]+)"', d)
            if m:
                return m.group(1)
            m = re.search(r"'text'\s*:\s*'([^']+)'", d)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def set_transcript_text(ev: Event, new_text: str) -> Event:
    """Return a new Event with transcript text replaced while preserving other fields."""
    d = ev.data or {}
    if isinstance(d, dict):
        d2 = dict(d)
        d2["text"] = new_text
        return Event(type=ev.type, data=d2)
    # fallback: create dict
    return Event(type=ev.type, data={"text": new_text})


# ------------------------------------------------------------
# Proxy piping
# ------------------------------------------------------------

ENGINE = RuleEngine(RULES_FILE)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str) -> None:
    """Read wyoming Events from reader and write to writer.
    For upstream_to_client, rewrite transcript and finish the turn after first transcript forwarded.
    """
    try:
        while True:
            ev = await async_read_event(reader)
            if ev is None:
                log.info("EOF: %s", direction)
                return

            log.debug("event %s: %s", direction, getattr(ev, "type", None))

            # Rewrite transcript on the way back
            if direction == "upstream_to_client" and getattr(ev, "type", None) == "transcript":
                original = extract_transcript_text(ev)
                if original is None:
                    # still forward as-is
                    await async_write_event(ev, writer)
                    await writer.drain()
                    # Even if text missing, treat transcript as end-of-turn
                    log.info("Transcript forwarded (no text); finishing upstream_to_client for this turn")
                    return

                fixed = ENGINE.apply(original)

                # Show normalization debug (helps confirm compact logic)
                log.debug(
                    "rewrite check: original=%r basic=%r compact=%r fixed=%r",
                    original,
                    normalize_basic(original),
                    normalize_compact(original),
                    fixed,
                )

                # If rewritten, replace in event
                if fixed != normalize_basic(original):
                    new_ev = set_transcript_text(ev, fixed)
                    log.info("Transcript rewrite: %r -> %r", normalize_basic(original), fixed)
                    await async_write_event(new_ev, writer)
                else:
                    await async_write_event(ev, writer)

                await writer.drain()

                # ✅ 핵심: transcript 1개면 이 턴은 끝 (EOF 기다리지 않음)
                log.info("Transcript forwarded; finishing upstream_to_client for this turn")
                return

            # passthrough
            await async_write_event(ev, writer)
            await writer.drain()

    except (ConnectionResetError, BrokenPipeError) as e:
        log.warning("pipe closed (%s): %s", direction, e)
        return
    except asyncio.CancelledError:
        log.info("pipe cancelled: %s", direction)
        raise
    except Exception as e:
        log.exception("pipe error (%s): %s", direction, e)
        return


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    peer = client_writer.get_extra_info("peername")
    log.info("Client connected: %s", peer)

    upstream_reader: asyncio.StreamReader
    upstream_writer: asyncio.StreamWriter
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    except Exception as e:
        log.error("Failed to connect upstream %s:%s: %s", UPSTREAM_HOST, UPSTREAM_PORT, e)
        client_writer.close()
        await client_writer.wait_closed()
        return

    t_in = asyncio.create_task(pipe(client_reader, upstream_writer, "client_to_upstream"))
    t_out = asyncio.create_task(pipe(upstream_reader, client_writer, "upstream_to_client"))

    try:
        # Wait for client->upstream to finish (audio-stop/EOF from HA side)
        await t_in

        # close upstream input (optional)
        try:
            upstream_writer.write_eof()
            await upstream_writer.drain()
        except Exception:
            pass

        # Wait for upstream_to_client to produce transcript (it returns right after first transcript)
        try:
            await asyncio.wait_for(t_out, timeout=TRANSCRIPT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            log.warning("Timeout waiting upstream_to_client transcript (no transcript within %.1fs)", TRANSCRIPT_TIMEOUT_SEC)
            t_out.cancel()
        except asyncio.CancelledError:
            raise

    finally:
        # Cleanup
        for t in (t_in, t_out):
            if not t.done():
                t.cancel()

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
    logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO").upper())

    server = await asyncio.start_server(handle_client, host=LISTEN_HOST, port=LISTEN_PORT)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    log.info("Listening on %s", addrs)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass




# import asyncio
# import os
# import time
# import logging
# import re
# import yaml

# from wyoming.event import async_read_event, async_write_event
# from wyoming.asr import Transcript

# LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
# log = logging.getLogger("wyoming-stt-proxy")

# LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
# LISTEN_PORT = int(os.getenv("LISTEN_PORT", "10301"))

# # 기본값을 docker-compose 네트워크 기준으로 변경
# UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "wyoming-whisper")
# UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "10300"))

# RULES_FILE = os.getenv("RULES_FILE", "/app/rules.yaml")

# MIN_REQUEST_INTERVAL_MS = int(os.getenv("MIN_REQUEST_INTERVAL_MS", "0"))  # 0이면 비활성

# # 연결별(세션별) 보호용 - 너무 빨리 재요청하면 upstream이 깨질 수 있어 간단한 쓰로틀
# _last_request_ms = 0


# def now_ms() -> int:
#     return int(time.time() * 1000)


# def normalize_basic(text: str) -> str:
#     """
#     기본 정규화:
#     - strip
#     - 공백 여러 개 -> 1개
#     """
#     if text is None:
#         return ""
#     t = text.strip()
#     t = re.sub(r"\s+", " ", t)
#     return t


# class RuleEngine:
#     def __init__(self, path: str):
#         self.path = path
#         self.mtime = 0.0
#         self.rules = []
#         self.load()

#     def load(self):
#         try:
#             st = os.stat(self.path)
#             self.mtime = st.st_mtime
#         except FileNotFoundError:
#             self.rules = []
#             return

#         with open(self.path, "r", encoding="utf-8") as f:
#             data = yaml.safe_load(f) or {}

#         self.rules = data.get("rules") or []

#     def reload_if_changed(self):
#         try:
#             st = os.stat(self.path)
#         except FileNotFoundError:
#             return

#         if st.st_mtime != self.mtime:
#             log.info("rules.yaml changed -> reload")
#             self.load()

#     def apply(self, text: str) -> str:
#         """
#         간단 규칙 적용 예시:
#         rules.yaml 형식에 맞춰 너가 이미 구현해둔 방식대로 동작.
#         여기서는 원본 파일 구현을 그대로 유지.
#         """
#         t0 = normalize_basic(text)
#         compact = re.sub(r"\s+", "", t0).lower()

#         for r in self.rules:
#             any_list = r.get("any") or []
#             out = r.get("set")

#             if not out:
#                 continue

#             # any: 공백/대소문자/기호 다소 무시한 비교를 위해 compact도 같이 본다
#             for a in any_list:
#                 if not a:
#                     continue
#                 a0 = normalize_basic(str(a))
#                 a_compact = re.sub(r"\s+", "", a0).lower()

#                 if a0 == t0 or a_compact == compact:
#                     return out

#         return t0


# engine = RuleEngine(RULES_FILE)


# async def pipe(reader, writer, direction: str):
#     """Wyoming 이벤트를 읽어서 그대로 write. transcript만 가로채서 바꿔치기."""
#     global _last_request_ms

#     try:
#         while True:
#             event = await async_read_event(reader)
#             if event is None:
#                 log.info("EOF: %s", direction)
#                 return

#             # (원인 추적용) 이벤트 타입 찍기 - 필요 없으면 나중에 지워도 됨
#             log.debug("event %s: %s", direction, getattr(event, "type", None))

#             # ✅ HA→proxy→whisper 방향: 오디오 종료는 보통 EOF가 아니라 audio-stop 이벤트로 온다.
#             #    audio-stop을 넘긴 뒤에는 더 이상 upstream에 보낼 입력이 없다고 보고 pipe를 종료한다.
#             if direction == "client_to_upstream":
#                 ev_type = getattr(event, "type", None)
#                 if ev_type in ("audio-stop", "audio_stop", "audioStop", "audio-end", "audio_end"):
#                     await async_write_event(event, writer)
#                     await writer.drain()
#                     log.info("Audio stop forwarded; finishing client_to_upstream")
#                     return

#             # 업스트림 -> í´라이언트(HA) 방향에서 transcript만 가로채기
#             if direction == "upstream_to_client" and event.type == "transcript":
#                 try:
#                     tr = Transcript.from_event(event)
#                     original = (getattr(tr, "text", None) or getattr(tr, "t", None) or "").strip()
#                     log.debug("transcript raw fields: text=%r t=%r", getattr(tr, "text", None), getattr(tr, "t", None))
#                     # original = tr.t or ""
#                 except Exception:
#                     original = None

#                 if original is not None:
#                     engine.reload_if_changed()
#                     fixed = engine.apply(original)

#                     if MIN_REQUEST_INTERVAL_MS > 0:
#                         now = now_ms()
#                         if now - _last_request_ms >= MIN_REQUEST_INTERVAL_MS:
#                             _last_request_ms = now

#                     if fixed != normalize_basic(original):
#                         log.info("Transcript rewrite: '%s' -> '%s'", original, fixed)
#                         event = Transcript(text=fixed).event()
#                     elif fixed != original:
#                         log.info("Transcript normalize: '%s' -> '%s'", original, fixed)
#                         event = Transcript(text=fixed).event()

#             await async_write_event(event, writer)
#             await writer.drain()

#             # ✅ 근본 수정: upstream(whisper)이 연결을 유지하더라도 transcript 1개면 턴 종료로 본다.
#            #    (EOF를 기다리면 timeout으로 취소되어 HA에 전달이 불안정해질 수 있음)
#             if direction == "upstream_to_client" and getattr(event, "type", None) == "transcript":
#                 log.info("Transcript forwarded; finishing upstream_to_client for this turn")
#                 return

#     except (ConnectionResetError, BrokenPipeError) as e:
#         log.warning("pipe closed (%s): %s", direction, e)
#         return
#     except asyncio.CancelledError:
#         log.info("pipe cancelled: %s", direction)
#         raise
#     except Exception as e:
#         log.exception("pipe error (%s): %s", direction, e)
#         return


# async def handle_client(client_reader, client_writer):
#     peer = client_writer.get_extra_info("peername")
#     log.info("Client connected: %s", peer)

#     try:
#         upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
#     except Exception as e:
#         log.error("Upstream connect failed %s:%d - %s", UPSTREAM_HOST, UPSTREAM_PORT, e)
#         client_writer.close()
#         await client_writer.wait_closed()
#         return

#     t_in = asyncio.create_task(pipe(client_reader, upstream_writer, "client_to_upstream"))
#     t_out = asyncio.create_task(pipe(upstream_reader, client_writer, "upstream_to_client"))

#     try:
#         # ✅ 입력(오디오)이 끝나는 걸 먼저 기다린다.
#         #    (이제는 EOF가 아니라 audio-stop에서 pipe가 return될 수 있다)
#         await t_in

#         # ✅ 입력이 끝났으면 upstream에 half-close(입력 종료) 알림
#         try:
#             upstream_writer.write_eof()
#             await upstream_writer.drain()
#         except Exception:
#             pass

#         # ✅ 여기서 절대 t_out을 cancel하지 말고, 결과가 올 때까지 기다린다.
#         try:
#             await asyncio.wait_for(t_out, timeout=30)
#         except asyncio.TimeoutError:
#             log.warning("Timeout waiting upstream_to_client transcript")

#     except asyncio.CancelledError:
#         raise
#     finally:
#         # 남아있으면 정리
#         for t in (t_in, t_out):
#             if not t.done():
#                 t.cancel()

#         try:
#             upstream_writer.close()
#             await upstream_writer.wait_closed()
#         except Exception:
#             pass

#         try:
#             client_writer.close()
#             await client_writer.wait_closed()
#         except Exception:
#             pass

#         log.info("Client disconnected: %s", peer)


# async def main():
#     server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
#     addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
#     log.info("Listening on %s", addrs)
#     async with server:
#         await server.serve_forever()


# if __name__ == "__main__":
#     asyncio.run(main())



# import asyncio
# import os
# import time
# import logging
# import re
# import yaml

# from wyoming.event import async_read_event, async_write_event
# from wyoming.asr import Transcript

# LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
# log = logging.getLogger("wyoming-stt-proxy")

# LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
# LISTEN_PORT = int(os.getenv("LISTEN_PORT", "10301"))

# UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "host.docker.internal")
# UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "10300"))

# RULES_FILE = os.getenv("RULES_FILE", "/app/rules.yaml")

# MIN_REQUEST_INTERVAL_MS = int(os.getenv("MIN_REQUEST_INTERVAL_MS", "0"))  # 0이면 비활성

# # 연결별(세션별) 보호용 - 너무 빨리 재요청하면 upstream이 깨질 수 있어 간단한 쓰로틀
# _last_request_ms = 0


# def now_ms() -> int:
#     return int(time.time() * 1000)


# def normalize_basic(text: str) -> str:
#     """기본 정리: 공백/구두점 정리"""
#     t = (text or "").strip()
#     # 구두점 -> 공백
#     t = re.sub(r"[,\.\?\!]+", " ", t)
#     # 공백 정리
#     t = re.sub(r"\s+", " ", t).strip()
#     return t

# def normalize_compact(text: str) -> str:
#     # 기본 정규화
#     t = normalize_basic(text)
#     # 공백/기호 제거: 한글/영문/숫자만 남김
#     t = re.sub(r"[^0-9a-zA-Z가-힣]", "", t)
#     # 소문자 통일
#     return t.lower()


# class RuleEngine:
#     def __init__(self, rules_path: str):
#         self.rules_path = rules_path
#         self.rules = []
#         self._mtime: float = 0.0   # ✅ 추가
#         self.load(force=True)

#     # def load(self):
#     def load(self, force: bool = False) -> None:
#         """rules.yaml을 읽어서 self.rules에 반영"""
#         try:
#             mtime = os.path.getmtime(self.rules_path)
#         except FileNotFoundError:
#             if force:
#                 log.error("Rules file not found: %s", self.rules_path)
#                 self.rules = []
#             return

#         if (not force) and (mtime == self._mtime):
#             return  # ✅ 변경 없으면 아무 것도 안 함

#         try:
#             with open(self.rules_path, "r", encoding="utf-8") as f:
#                 data = yaml.safe_load(f) or {}
#             self.rules = data.get("rules", []) or []
#             self._mtime = mtime
#             log.info("Rules reloaded: %d rules from %s", len(self.rules), self.rules_path)
#         except Exception as e:
#             log.error("Failed to load rules from %s: %s", self.rules_path, e)
#             # 실패 시 기존 rules 유지하고 싶으면 아래 줄은 주석 처리해도 됨
#             # self.rules = []

#         # try:
#         #     with open(self.rules_path, "r", encoding="utf-8") as f:
#         #         data = yaml.safe_load(f) or {}
#         #     self.rules = data.get("rules", []) or []
#         #     log.info("Loaded %d rules from %s", len(self.rules), self.rules_path)
#         # except Exception as e:
#         #     log.error("Failed to load rules from %s: %s", self.rules_path, e)
#         #     self.rules = []

#     def reload_if_changed(self) -> None:
#         """매 요청마다 호출해도 부담 거의 없는 hot-reload"""
#         self.load(force=False)

#     def apply(self, text: str) -> str:
#         t0 = normalize_basic(text)

#         # 비교용: 공백/기호 제거한 버전
#         compact = normalize_compact(text)

#         for r in self.rules:
#             any_list = r.get("any") or []
#             out = r.get("set")
#             if not out:
#                 continue

#             for needle in any_list:
#                 n0 = normalize_basic(str(needle))
#                 ncompact = normalize_compact(str(needle))

#                 # 1) 컴팩트 포함 (전등꺼 / 전 등 꺼 / 전, 들, 고 / 전.. 켜? 등 흡수)
#                 if ncompact and ncompact in compact:
#                     return out

#                 # 2) 원문 포함 (혹시라도 원문 기반 룰 쓰고 있으면 유지)
#                 if n0 and n0 in t0:
#                     return out

#         return t0

#     # def apply(self, text: str) -> str:
#     #     t0 = normalize_basic(text)

#     #     # 비교용으로 공백/소문자/기호 조금 더 정리한 버전도 준비
#     #     # compact = re.sub(r"\s+", "", t0).lower()
#     #     compact = normalize_compact(text)

#     #     for r in self.rules:
#     #         any_list = r.get("any") or []
#     #         out = r.get("set")
#     #         if not out:
#     #             continue

#     #         for needle in any_list:
#     #             n0 = normalize_basic(str(needle))
#     #             ncompact = re.sub(r"\s+", "", n0).lower()

#     #             # 1) 컴팩트 일치 (전등꺼 / 전 등 꺼 / 전,들,고 등 변형 흡수)
#     #             if ncompact and ncompact in compact:
#     #                 return out

#     #             # 2) 원문 포함
#     #             if n0 and n0 in t0:
#     #                 return out

#     #     return t0


# engine = RuleEngine(RULES_FILE)

# async def pipe(reader, writer, direction: str):
#     """Wyoming 이벤트를 읽어서 그대로 write. transcript만 가로채서 바꿔치기."""
#     global _last_request_ms

#     try:
#         while True:
#             event = await async_read_event(reader)
#             if event is None:
#                 log.info("EOF: %s", direction)
#                 return

#             # (원인 추적용) 이벤트 타입 찍기 - 필요 없으면 나중에 지워도 됨
#             log.debug("event %s: %s", direction, getattr(event, "type", None))

#             # 업스트림 -> 클라이언트(HA) 방향에서 transcript만 가로채기
#             if direction == "upstream_to_client" and event.type == "transcript":
#                 try:
#                     tr = Transcript.from_event(event)
#                     original = tr.text or ""
#                 except Exception:
#                     original = None

#                 if original is not None:
#                     engine.reload_if_changed()
#                     fixed = engine.apply(original)

#                     if MIN_REQUEST_INTERVAL_MS > 0:
#                         now = now_ms()
#                         if now - _last_request_ms >= MIN_REQUEST_INTERVAL_MS:
#                             _last_request_ms = now

#                     if fixed != normalize_basic(original):
#                         log.info("Transcript rewrite: '%s' -> '%s'", original, fixed)
#                         event = Transcript(text=fixed).event()
#                     elif fixed != original:
#                         log.info("Transcript normalize: '%s' -> '%s'", original, fixed)
#                         event = Transcript(text=fixed).event()

#             await async_write_event(event, writer)
#             await writer.drain()

#     except (ConnectionResetError, BrokenPipeError) as e:
#         log.warning("pipe closed (%s): %s", direction, e)
#         return
#     except asyncio.CancelledError:
#         log.info("pipe cancelled: %s", direction)
#         raise
#     except Exception as e:
#         log.exception("pipe error (%s): %s", direction, e)
#         return


# # async def pipe(reader, writer, direction: str):
# #     """Wyoming 이벤트를 읽어서 그대로 write. transcript만 가로채서 바꿔치기."""
# #     global _last_request_ms

# #     while True:
# #         event = await async_read_event(reader)
# #         if event is None:
# #             return

# #         # 업스트림 -> 클라이언트(HA) 방향에서 transcript만 가로채기
# #         if direction == "upstream_to_client" and event.type == "transcript":
# #             try:
# #                 tr = Transcript.from_event(event)
# #                 original = tr.text or ""
# #             except Exception:
# #                 # 버전/포맷 차이 나면 그냥 통과
# #                 original = None

# #             if original is not None:
# #                 engine.reload_if_changed()
# #                 fixed = engine.apply(original)

# #                 # 너무 빠른 연속요청 완화(선택)
# #                 if MIN_REQUEST_INTERVAL_MS > 0:
# #                     now = now_ms()
# #                     if now - _last_request_ms < MIN_REQUEST_INTERVAL_MS:
# #                         # 너무 촘촘하면 transcript는 그대로 두고 통과
# #                         pass
# #                     else:
# #                         _last_request_ms = now

# #                 if fixed != normalize_basic(original):
# #                     log.info("Transcript rewrite: '%s' -> '%s'", original, fixed)
# #                     event = Transcript(text=fixed).event()
# #                 else:
# #                     # 기본정리만 반영(예: 앞 공백 제거 같은 것)
# #                     if fixed != original:
# #                         log.info("Transcript normalize: '%s' -> '%s'", original, fixed)
# #                         event = Transcript(text=fixed).event()

# #         await async_write_event(event, writer)
# #         await writer.drain()

# async def handle_client(client_reader, client_writer):
#     peer = client_writer.get_extra_info("peername")
#     log.info("Client connected: %s", peer)

#     try:
#         upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
#     except Exception as e:
#         log.error("Upstream connect failed %s:%d - %s", UPSTREAM_HOST, UPSTREAM_PORT, e)
#         client_writer.close()
#         await client_writer.wait_closed()
#         return

#     t_in = asyncio.create_task(pipe(client_reader, upstream_writer, "client_to_upstream"))
#     t_out = asyncio.create_task(pipe(upstream_reader, client_writer, "upstream_to_client"))

#     try:
#         # ✅ 입력(오디오)이 끝나는 걸 먼저 기다린다.
#         await t_in

#         # ✅ 입력이 끝났으면 upstream에 half-close(입력 종료) 알림
#         try:
#             upstream_writer.write_eof()
#             await upstream_writer.drain()
#         except Exception:
#             pass

#         # ✅ 여기서 절대 t_out을 cancel하지 말고, 결과가 올 때까지 기다린다.
#         # await t_out
#         try:
#             await asyncio.wait_for(t_out, timeout=30)
#         except asyncio.TimeoutError:
#             log.warning("Timeout waiting upstream_to_client transcript")

#     except asyncio.CancelledError:
#         raise
#     finally:
#         # 남아있으면 정리
#         for t in (t_in, t_out):
#             if not t.done():
#                 t.cancel()

#         try:
#             upstream_writer.close()
#             await upstream_writer.wait_closed()
#         except Exception:
#             pass

#         try:
#             client_writer.close()
#             await client_writer.wait_closed()
#         except Exception:
#             pass

#         log.info("Client disconnected: %s", peer)


# # async def handle_client(client_reader, client_writer):
# #     peer = client_writer.get_extra_info("peername")
# #     log.info("Client connected: %s", peer)

# #     try:
# #         upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
# #     except Exception as e:
# #         log.error("Upstream connect failed %s:%d - %s", UPSTREAM_HOST, UPSTREAM_PORT, e)
# #         client_writer.close()
# #         await client_writer.wait_closed()
# #         return

# #     try:
# #         t1 = asyncio.create_task(pipe(client_reader, upstream_writer, "client_to_upstream"))
# #         t2 = asyncio.create_task(pipe(upstream_reader, client_writer, "upstream_to_client"))

# #         done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
# #         for p in pending:
# #             p.cancel()
# #     finally:
# #         try:
# #             upstream_writer.close()
# #             await upstream_writer.wait_closed()
# #         except Exception:
# #             pass
# #         try:
# #             client_writer.close()
# #             await client_writer.wait_closed()
# #         except Exception:
# #             pass
# #         log.info("Client disconnected: %s", peer)


# async def main():
#     server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
#     addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
#     log.info("Listening on %s; upstream=%s:%d; rules=%s", addrs, UPSTREAM_HOST, UPSTREAM_PORT, RULES_FILE)

#     async with server:
#         await server.serve_forever()


# if __name__ == "__main__":
#     asyncio.run(main())

