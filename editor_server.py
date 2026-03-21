#!/usr/bin/env python3
"""
소설 편집 리뷰 MCP Server
- 외부 AI(Gemini CLI, Codex CLI, NIM Proxy, Ollama)를 호출하여 에피소드 편집 리뷰를 수행
- review_episode: 단건 리뷰
- batch_review: 일괄 리뷰 (정기 점검 P7용)
- check_status: 외부 AI 서비스 상태 확인
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from compile_brief import register_compile_brief

mcp = FastMCP("novel-editor")

NOVEL_ROOT = os.getenv("NOVEL_ROOT", "/root/novel")
NIM_PROXY_URL = os.getenv("NIM_PROXY_URL", "http://localhost:8082")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
NIM_TIMEOUT = int(os.getenv("NIM_TIMEOUT", "600"))
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "900"))
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "600"))
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "600"))


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
        "gemini_feedback": True,
        "gpt_feedback": True,
        "nim_feedback": False,
        "nim_feedback_model": "openai/gpt-oss-120b",
        "ollama_feedback": False,
        "ollama_feedback_model": "openai/gpt-oss-120b",
        "illustration": False,
    }
    for key in flags:
        # 두 가지 형식 지원: "**key**: value" 또는 "| **key** | value |" (마크다운 테이블)
        m = re.search(rf'\*\*{key}\*\*\s*\|?\s*[:]*\s*(true|false|"[^"]*"|[^\s|]+)', content, re.IGNORECASE)
        if not m:
            # 테이블 형식: | **key** | value |
            m = re.search(rf'\|\s*\*\*{key}\*\*\s*\|\s*(true|false|"[^"]*"|[^\s|]+)', content, re.IGNORECASE)
        if m:
            val = m.group(1).strip('"')
            if val.lower() == "true":
                flags[key] = True
            elif val.lower() == "false":
                flags[key] = False
            else:
                flags[key] = val
    return flags


def build_prompt(episode_file: str) -> str:
    """NIM/Ollama용 교정 프롬프트를 구성한다 (맞춤법/문법/어색한 표현만)."""
    episode = safe_read(episode_file)
    # EPISODE_META 제거
    episode = re.split(r"^---\s*\n### EPISODE_META", episode, flags=re.MULTILINE)[0].strip()

    prompt = """아래 소설 에피소드의 **한글 교정 + 대사 맥락 검토**를 해줘.

## 검토 범위
1. **맞춤법 오류**: 띄어쓰기, 오탈자, 잘못된 조사 사용
2. **어색한 표현**: 부자연스러운 접두어/접미어, 번역투, 어색한 어순
3. **문법 오류**: 주술 호응, 시제 불일치, 조사 중복
4. **대사 맥락 이상**: 에피소드 내에서 대사의 흐름이 앞뒤가 안 맞거나, 같은 캐릭터가 갑자기 어투가 바뀌거나, 대화 맥락이 이상한 경우

## 검토하지 않는 것 (무시해)
- 전체 스토리 아크, 세계관 설정, 복선 등 고차원 서사 리뷰
- 문체 제안, 표현력 향상 등 주관적 의견

## 출력 형식
오류가 있는 경우만 아래 형식으로 출력:

| # | 유형 | 원문 | 수정 제안 |
|---|------|------|----------|
| 1 | 맞춤법 | "원문 발췌" | "수정안" |
| 2 | 대사맥락 | "해당 대사" | "이상한 이유 설명" |

오류가 없으면 "교정 사항 없음"이라고만 출력해.

[검토 대상]
"""
    prompt += episode
    return prompt


def save_feedback(novel_dir: str, source: str, content: str, episode_file: str, skip_if_exists: bool = False):
    """EDITOR_FEEDBACK_{source}.md에 피드백을 저장한다. 메타데이터 헤더를 보장.

    기존 피드백이 있으면 archive/ 폴더로 이동하여 유실을 방지한다.
    """
    filename = f"EDITOR_FEEDBACK_{source}.md"
    filepath = os.path.join(novel_dir, filename)
    ep_name = os.path.basename(episode_file)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"<!-- source: {source} | file: {ep_name} | date: {timestamp} -->\n\n"

    if skip_if_exists and os.path.exists(filepath):
        # CLI가 직접 파일을 썼을 수 있음 — 메타데이터 헤더가 없으면 prepend
        existing = Path(filepath).read_text(encoding="utf-8")
        if not existing.startswith("<!-- source:"):
            Path(filepath).write_text(header + existing, encoding="utf-8")
        return filepath

    # 기존 피드백이 있으면 archive로 이동 (유실 방지)
    if os.path.exists(filepath):
        archive_dir = os.path.join(novel_dir, "feedback-archive")
        os.makedirs(archive_dir, exist_ok=True)
        existing = Path(filepath).read_text(encoding="utf-8")
        # 기존 파일에서 에피소드명 추출
        import re
        old_ep_match = re.search(r"file: ([^|]+)", existing)
        old_ep = old_ep_match.group(1).strip() if old_ep_match else "unknown"
        old_ep_stem = old_ep.replace(".md", "")
        archive_name = f"EDITOR_FEEDBACK_{source}_{old_ep_stem}.md"
        archive_path = os.path.join(archive_dir, archive_name)
        Path(archive_path).write_text(existing, encoding="utf-8")

    Path(filepath).write_text(header + content, encoding="utf-8")
    return filepath


# ─── Source Callers ─────────────────────────────────────────

async def call_nim(prompt: str, model: str) -> str:
    """NIM Proxy를 통해 맞춤법/문법 교정을 받는다 (SSE 스트리밍)."""
    body = {
        "model": model,
        "max_tokens": 16384,
        "system": "당신은 한국어 맞춤법과 문법 교정 전문가입니다. 지적된 형식에 맞춰 간결하게 답변하세요.",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.3,
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


async def call_ollama(prompt: str, model: str) -> str:
    """Ollama API로 맞춤법/문법 교정을 받는다."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "당신은 한국어 맞춤법과 문법 교정 전문가입니다. 지적된 형식에 맞춰 간결하게 답변하세요."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"num_predict": 4096, "temperature": 0.3},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(OLLAMA_TIMEOUT, connect=10)) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("message", {}).get("content", "")


async def call_gemini(novel_dir: str, episode_file: str) -> str:
    """Gemini CLI로 리뷰를 받는다 (파일시스템 접근)."""
    ep_path = episode_file
    gemini_md = os.path.join(os.path.dirname(__file__), "GEMINI.md")
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = f"오늘 날짜는 {today}이다. {gemini_md}에 따라 {ep_path}를 리뷰해. 설정 파일(settings/), 요약(summaries/), 이전 피드백 로그(summaries/editor-feedback-log.md)를 참고해서 EDITOR_FEEDBACK_gemini.md에 결과를 작성해."

    proc = await asyncio.create_subprocess_exec(
        "gemini", "-m", "gemini-3.1-pro-preview", "-p", "-", "-y",
        cwd=novel_dir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=GEMINI_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
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


async def call_codex(novel_dir: str, episode_file: str) -> str:
    """Codex CLI (GPT)로 리뷰를 받는다 (파일시스템 접근)."""
    ep_path = episode_file
    gpt_md = os.path.join(os.path.dirname(__file__), "GPT.md")
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = f"오늘 날짜는 {today}이다. {gpt_md}에 따라 {ep_path}를 리뷰해. 설정 파일(settings/), 요약(summaries/), 이전 피드백 로그(summaries/editor-feedback-log.md)를 참고해서 EDITOR_FEEDBACK_gpt.md에 결과를 작성해."

    proc = await asyncio.create_subprocess_exec(
        "codex", "exec", "-", "--full-auto", "-m", "gpt-5.4", "-C", novel_dir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=CODEX_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Codex CLI 시간 초과")

    # Codex가 직접 파일에 썼는지 확인
    feedback_path = os.path.join(novel_dir, "EDITOR_FEEDBACK_gpt.md")
    if os.path.exists(feedback_path):
        return safe_read(feedback_path)

    # stdout에 출력한 경우
    output = stdout.decode("utf-8", errors="replace").strip()
    if output:
        return output

    raise RuntimeError(f"Codex 리뷰 실패: {stderr.decode('utf-8', errors='replace')[:300]}")


async def call_codex_naturalness(novel_dir: str, episode_file: str) -> str:
    """GPT로 결합 자연성만 전문 검사한다."""
    ep_path = episode_file
    prompt = f"""아래 한국어 소설 에피소드를 읽고, **결합 자연성**만 검사해줘.

점검 대상: 명사-동사, 명사-형용사, 부사-동사, 감정-신체, 감각-동작, 추상명사-서술어 결합.

핵심 기준: 문법적으로 가능하더라도, 한국어 화자가 같은 의미에서 보통 택하지 않는 결합이면 지적.

판정 질문:
1. 이 표현을 한국어 화자가 자연스럽게 쓸 가능성이 높은가?
2. 같은 뜻에서 더 관용적인 결합이 따로 있는가?
3. 어색함이 결합 방식 자체에서 오는가?

예외: 시적 비유, 장르적 낯설게 쓰기, 캐릭터 고유 어법은 허용.

출력: 마크다운 테이블로. 결함 없으면 "결함 없음".

| # | 위치(줄) | 원문 | 왜 어색한가 | 자연한 대안 |
|---|---------|------|-----------|-----------|

파일: {ep_path}"""

    proc = await asyncio.create_subprocess_exec(
        "codex", "exec", "-", "--full-auto", "-m", "gpt-5.4", "-C", novel_dir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=CODEX_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("GPT 결합 자연성 검사 시간 초과")

    output = stdout.decode("utf-8", errors="replace").strip()
    if output:
        return output

    raise RuntimeError(f"GPT 결합 자연성 검사 실패: {stderr.decode('utf-8', errors='replace')[:300]}")


# ─── MCP Tools ──────────────────────────────────────────────

@mcp.tool()
async def review_episode(
    episode_file: str,
    novel_dir: str,
    sources: str = "auto",
) -> str:
    """에피소드 편집 리뷰를 외부 AI에 요청합니다.

    NIM Proxy, Ollama, Gemini CLI, Codex CLI(GPT)를 호출하여 EDITOR_FEEDBACK_*.md를 생성합니다.
    각 소스의 활성화 여부는 CLAUDE.md의 플래그를 따르거나 sources 파라미터로 직접 지정할 수 있습니다.

    Args:
        episode_file: 에피소드 파일 절대 경로
        novel_dir: 소설 폴더 절대 경로
        sources: "auto" (CLAUDE.md 플래그 따름), "gemini", "gpt", "nim", "ollama", "all", 또는 쉼표 구분 조합
    """
    if not os.path.isfile(episode_file):
        return f"❌ 에피소드 파일이 없습니다: {episode_file}"
    if not os.path.isdir(novel_dir):
        return f"❌ 소설 폴더가 없습니다: {novel_dir}"

    flags = parse_claude_md(novel_dir)
    ep_name = os.path.basename(episode_file)

    # 활성 소스 결정
    if sources == "auto":
        active = set()
        if flags["gemini_feedback"]:
            active.add("gemini")
        if flags["gpt_feedback"]:
            active.add("gpt")
        if flags["nim_feedback"]:
            active.add("nim")
        if flags["ollama_feedback"]:
            active.add("ollama")
    elif sources == "all":
        active = {"nim", "ollama", "gemini", "gpt"}
    else:
        active = {s.strip() for s in sources.split(",")}

    if not active:
        return f"## 편집 리뷰 건너뜀: {ep_name}\n\n모든 피드백 소스가 비활성화되어 있습니다. CLAUDE.md의 플래그를 확인하세요."

    results = {}
    errors = {}

    # 모든 소스를 독립 병렬로 실행
    all_tasks = []
    if "nim" in active or "ollama" in active:
        prompt = build_prompt(episode_file)
    if "nim" in active:
        all_tasks.append(("nim", call_nim(prompt, flags["nim_feedback_model"])))
    if "ollama" in active:
        all_tasks.append(("ollama", call_ollama(prompt, flags["ollama_feedback_model"])))
    if "gemini" in active:
        all_tasks.append(("gemini", call_gemini(novel_dir, episode_file)))
    if "gpt" in active:
        all_tasks.append(("gpt", call_codex(novel_dir, episode_file)))
    if "gpt_naturalness" in active:
        all_tasks.append(("gpt_naturalness", call_codex_naturalness(novel_dir, episode_file)))

    if all_tasks:
        gathered = await asyncio.gather(
            *[task for _, task in all_tasks],
            return_exceptions=True,
        )
        for (source_name, _), result in zip(all_tasks, gathered):
            if isinstance(result, Exception):
                errors[source_name] = str(result)
            else:
                results[source_name] = result
                save_feedback(novel_dir, source_name, result, episode_file,
                              skip_if_exists=(source_name in ("gemini", "gpt")))

    # 결과 요약
    lines = [f"## 편집 리뷰 결과: {ep_name}\n"]
    for src in ["nim", "ollama", "gemini", "gpt", "gpt_naturalness"]:
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
        if proc.returncode == 0 and path:
            rows.append(f"| Gemini CLI | ✅ 설치됨 | `{path}` |")
        else:
            rows.append("| Gemini CLI | ❌ 미설치 | `npm install -g @google/gemini-cli` |")
    except Exception:
        rows.append("| Gemini CLI | ❌ 미설치 | `npm install -g @google/gemini-cli` |")

    # Codex CLI
    try:
        proc = await asyncio.create_subprocess_exec(
            "which", "codex",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        path = stdout.decode().strip()
        if proc.returncode == 0 and path:
            rows.append(f"| Codex CLI (GPT) | ✅ 설치됨 | `{path}` |")
        else:
            rows.append("| Codex CLI (GPT) | ❌ 미설치 | `npm install -g @openai/codex` |")
    except Exception:
        rows.append("| Codex CLI (GPT) | ❌ 미설치 | `npm install -g @openai/codex` |")

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


register_compile_brief(mcp)

if __name__ == "__main__":
    mcp.run()
