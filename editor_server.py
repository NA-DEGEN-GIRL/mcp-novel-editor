#!/usr/bin/env python3
"""
소설 편집 리뷰 MCP Server
- 외부 AI(Gemini CLI, NIM Proxy, Ollama)를 호출하여 에피소드 편집 리뷰를 수행
- review_episode: 단건 리뷰
- batch_review: 일괄 리뷰 (정기 점검 P7용)
- check_status: 외부 AI 서비스 상태 확인
"""

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("novel-editor")

NOVEL_ROOT = os.getenv("NOVEL_ROOT", "/root/novel")
NIM_PROXY_URL = os.getenv("NIM_PROXY_URL", "http://localhost:8082")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_PATH = os.getenv("OLLAMA_PATH", "/usr/local/bin/ollama")
NIM_TIMEOUT = int(os.getenv("NIM_TIMEOUT", "600"))
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "900"))
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "600"))


# ─── Helpers ────────────────────────────────────────────────

def safe_read(path: str, max_lines: int = 0) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
        if max_lines > 0:
            lines = text.splitlines()
            if len(lines) > max_lines:
                text = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines}줄 생략)"
        return text
    except (FileNotFoundError, PermissionError):
        return ""


def parse_claude_md(novel_dir: str) -> dict:
    """CLAUDE.md에서 피드백 플래그와 모델명을 파싱한다."""
    content = safe_read(os.path.join(novel_dir, "CLAUDE.md"))
    flags = {
        "nim_feedback": False,
        "nim_feedback_model": "mistralai/mistral-large-3-675b-instruct-2512",
        "ollama_feedback": False,
        "ollama_feedback_model": "gpt-oss:120b",
        "illustration": False,
    }
    for key in flags:
        m = re.search(rf'\*\*{key}\*\*:\s*(true|false|"[^"]*")', content, re.IGNORECASE)
        if m:
            val = m.group(1).strip('"')
            if val.lower() == "true":
                flags[key] = True
            elif val.lower() == "false":
                flags[key] = False
            else:
                flags[key] = val
    return flags


def build_prompt(novel_dir: str, episode_file: str, other_reviews: dict = None) -> str:
    """NIM/Ollama용 리뷰 프롬프트를 구성한다 (설정 파일 임베딩)."""
    episode = safe_read(episode_file)
    # EPISODE_META 제거
    episode = re.split(r"^---\s*\n### EPISODE_META", episode, flags=re.MULTILINE)[0].strip()

    style = safe_read(os.path.join(novel_dir, "settings/01-style-guide.md"), 100)
    chars = safe_read(os.path.join(novel_dir, "settings/03-characters.md"), 150)
    world = safe_read(os.path.join(novel_dir, "settings/04-worldbuilding.md"), 80)

    prompt = "아래 소설 에피소드를 리뷰해. 결과는 EDITOR_FEEDBACK 형식으로 출력해.\n\n"
    if style:
        prompt += f"[설정 참고]\n문체 가이드:\n{style}\n\n"
    if chars:
        prompt += f"캐릭터:\n{chars}\n\n"
    if world:
        prompt += f"세계관:\n{world}\n\n"

    if other_reviews:
        for source, content in other_reviews.items():
            if content:
                truncated = content[:3000] if len(content) > 3000 else content
                prompt += f"[다른 AI 리뷰 참고 - {source}]\n{truncated}\n\n"

    prompt += f"[리뷰 대상]\n{episode}"
    return prompt


def get_system_prompt() -> str:
    """GEMINI.md를 시스템 프롬프트로 읽는다."""
    return safe_read(os.path.join(NOVEL_ROOT, "GEMINI.md"))


def save_feedback(novel_dir: str, source: str, content: str, episode_file: str):
    """EDITOR_FEEDBACK_{source}.md에 피드백을 저장한다."""
    filename = f"EDITOR_FEEDBACK_{source}.md"
    filepath = os.path.join(novel_dir, filename)
    ep_name = os.path.basename(episode_file)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"<!-- source: {source} | file: {ep_name} | date: {timestamp} -->\n\n"
    Path(filepath).write_text(header + content, encoding="utf-8")
    return filepath


# ─── Source Callers ─────────────────────────────────────────

async def call_nim(prompt: str, system: str, model: str) -> str:
    """NIM Proxy를 통해 리뷰를 받는다 (SSE 스트리밍)."""
    body = {
        "model": model,
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    headers = {"x-api-key": "dummy", "anthropic-version": "2023-06-01"}

    full_text = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(NIM_TIMEOUT, connect=10)) as client:
        async with client.stream("POST", f"{NIM_PROXY_URL}/v1/messages", json=body, headers=headers) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise RuntimeError(f"NIM HTTP {resp.status_code}: {resp.text[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                etype = data.get("type", "")
                if etype == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        full_text += delta.get("text", "")
                elif etype == "message_stop":
                    break
    return full_text


async def call_ollama(prompt: str, system: str, model: str) -> str:
    """Ollama API로 리뷰를 받는다."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(OLLAMA_TIMEOUT, connect=10)) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("message", {}).get("content", "")


async def call_gemini(novel_dir: str, episode_file: str, other_reviews: dict = None) -> str:
    """Gemini CLI로 리뷰를 받는다 (파일시스템 접근)."""
    ep_path = episode_file
    prompt = f"/root/novel/GEMINI.md에 따라 {ep_path}를 리뷰해. 설정 파일(settings/), 요약(summaries/), 이전 피드백 로그(summaries/editor-feedback-log.md)를 참고해서 EDITOR_FEEDBACK_gemini.md에 결과를 작성해."

    if other_reviews:
        refs = []
        for source, content in other_reviews.items():
            if content:
                truncated = content[:2000] if len(content) > 2000 else content
                refs.append(f"[{source} 리뷰]\n{truncated}")
        if refs:
            prompt += f"\n\n추가 참고 자료: 아래 다른 AI 모델의 리뷰 결과도 참고하되, 맹신하지 말고 네 독자적 판단으로 리뷰해.\n" + "\n".join(refs)

    proc = await asyncio.create_subprocess_exec(
        "gemini", "-p", prompt, "-y",
        cwd=novel_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GEMINI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Gemini CLI 시간 초과")

    # Gemini가 직접 파일에 썼는지 확인
    feedback_path = os.path.join(novel_dir, "EDITOR_FEEDBACK_gemini.md")
    if os.path.exists(feedback_path):
        return safe_read(feedback_path)

    # stdout에 출력한 경우
    output = stdout.decode("utf-8", errors="replace").strip()
    if output:
        return output

    raise RuntimeError(f"Gemini 리뷰 실패: {stderr.decode('utf-8', errors='replace')[:300]}")


# ─── MCP Tools ──────────────────────────────────────────────

@mcp.tool()
async def review_episode(
    episode_file: str,
    novel_dir: str,
    sources: str = "auto",
) -> str:
    """에피소드 편집 리뷰를 외부 AI에 요청합니다.

    NIM Proxy, Ollama, Gemini CLI를 호출하여 EDITOR_FEEDBACK_*.md를 생성합니다.
    각 소스의 활성화 여부는 CLAUDE.md의 플래그를 따르거나 sources 파라미터로 직접 지정할 수 있습니다.

    Args:
        episode_file: 에피소드 파일 절대 경로
        novel_dir: 소설 폴더 절대 경로
        sources: "auto" (CLAUDE.md 플래그 따름), "gemini", "nim", "ollama", "all", 또는 쉼표 구분 조합
    """
    if not os.path.isfile(episode_file):
        return f"❌ 에피소드 파일이 없습니다: {episode_file}"
    if not os.path.isdir(novel_dir):
        return f"❌ 소설 폴더가 없습니다: {novel_dir}"

    flags = parse_claude_md(novel_dir)
    system = get_system_prompt()
    ep_name = os.path.basename(episode_file)

    # 활성 소스 결정
    if sources == "auto":
        active = set()
        if flags["nim_feedback"]:
            active.add("nim")
        if flags["ollama_feedback"]:
            active.add("ollama")
        active.add("gemini")
    elif sources == "all":
        active = {"nim", "ollama", "gemini"}
    else:
        active = {s.strip() for s in sources.split(",")}

    results = {}
    errors = {}

    # Phase 1: NIM + Ollama 병렬
    nim_ollama_tasks = []
    if "nim" in active:
        prompt = build_prompt(novel_dir, episode_file)
        nim_ollama_tasks.append(("nim", call_nim(prompt, system, flags["nim_feedback_model"])))
    if "ollama" in active:
        prompt = build_prompt(novel_dir, episode_file)
        nim_ollama_tasks.append(("ollama", call_ollama(prompt, system, flags["ollama_feedback_model"])))

    if nim_ollama_tasks:
        gathered = await asyncio.gather(
            *[task for _, task in nim_ollama_tasks],
            return_exceptions=True,
        )
        for (source_name, _), result in zip(nim_ollama_tasks, gathered):
            if isinstance(result, Exception):
                errors[source_name] = str(result)
            else:
                results[source_name] = result
                save_feedback(novel_dir, source_name, result, episode_file)

    # Phase 2: Gemini (NIM/Ollama 결과를 참고)
    if "gemini" in active:
        try:
            gemini_result = await call_gemini(novel_dir, episode_file, results)
            results["gemini"] = gemini_result
            save_feedback(novel_dir, "gemini", gemini_result, episode_file)
        except Exception as e:
            errors["gemini"] = str(e)
            # Gemini 실패 시 NIM fallback
            if "nim" not in results:
                try:
                    prompt = build_prompt(novel_dir, episode_file, results)
                    fallback = await call_nim(prompt, system, "mistralai/mistral-large-3-675b-instruct-2512")
                    results["gemini_fallback"] = fallback
                    save_feedback(novel_dir, "gemini", fallback, episode_file)
                    errors["gemini"] += " → NIM fallback 성공"
                except Exception as fe:
                    errors["gemini_fallback"] = str(fe)

    # 결과 요약
    lines = [f"## 편집 리뷰 결과: {ep_name}\n"]
    for src in ["nim", "ollama", "gemini", "gemini_fallback"]:
        if src in results:
            preview = results[src][:500].replace("\n", " ")
            path = os.path.join(novel_dir, f"EDITOR_FEEDBACK_{src.split('_')[0]}.md")
            lines.append(f"### ✅ {src.upper()}\n- 저장: `{path}`\n- 미리보기: {preview}...\n")
        elif src in errors:
            lines.append(f"### ❌ {src.upper()}\n- 오류: {errors[src]}\n")

    return "\n".join(lines)


@mcp.tool()
async def batch_review(
    episode_files: str,
    novel_dir: str,
    sources: str = "auto",
) -> str:
    """여러 에피소드를 일괄 리뷰합니다. 정기 점검(P7) 또는 아크 종료 시 사용합니다.

    Args:
        episode_files: 에피소드 파일 경로들 (쉼표 구분) 또는 범위 ("7-11")
        novel_dir: 소설 폴더 절대 경로
        sources: review_episode와 동일
    """
    if not os.path.isdir(novel_dir):
        return f"❌ 소설 폴더가 없습니다: {novel_dir}"

    # 파일 목록 파싱
    files = []
    if re.match(r"^\d+-\d+$", episode_files.strip()):
        start, end = map(int, episode_files.strip().split("-"))
        # chapters/ 내 모든 md 파일 탐색
        for md in sorted(Path(novel_dir).rglob("chapters/**/*.md")):
            m = re.search(r"chapter-(\d+)\.md$", str(md))
            if m:
                num = int(m.group(1))
                if start <= num <= end:
                    files.append(str(md))
    else:
        files = [f.strip() for f in episode_files.split(",") if f.strip()]

    if not files:
        return f"❌ 리뷰할 에피소드를 찾을 수 없습니다: {episode_files}"

    results = []
    for f in files:
        result = await review_episode(f, novel_dir, sources)
        results.append(result)

    return f"## 일괄 리뷰 완료 ({len(files)}개 에피소드)\n\n" + "\n---\n\n".join(results)


@mcp.tool()
async def check_status() -> str:
    """외부 AI 편집 리뷰 서비스의 상태를 확인합니다.

    Gemini CLI 설치 여부, NIM Proxy 연결, Ollama 연결 등을 체크합니다.
    """
    rows = []

    # Gemini CLI
    try:
        proc = await asyncio.create_subprocess_exec(
            "which", "gemini",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        path = stdout.decode().strip()
        rows.append(f"| Gemini CLI | ✅ 설치됨 | `{path}` |")
    except Exception:
        rows.append("| Gemini CLI | ❌ 미설치 | `npm install -g @google/gemini-cli` |")

    # NIM Proxy
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{NIM_PROXY_URL}/health")
            data = resp.json()
            model = data.get("model", "unknown")
            rows.append(f"| NIM Proxy | ✅ 연결됨 | `{NIM_PROXY_URL}` model=`{model}` |")
    except Exception as e:
        rows.append(f"| NIM Proxy | ❌ 오프라인 | `{NIM_PROXY_URL}` — {e} |")

    # Ollama
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
            model_list = ", ".join(models[:5]) if models else "모델 없음"
            rows.append(f"| Ollama | ✅ 연결됨 | `{OLLAMA_URL}` 모델: {model_list} |")
    except Exception as e:
        rows.append(f"| Ollama | ❌ 오프라인 | `{OLLAMA_URL}` — {e} |")

    header = "| 서비스 | 상태 | 상세 |\n|--------|------|------|\n"
    return header + "\n".join(rows)


if __name__ == "__main__":
    mcp.run()
