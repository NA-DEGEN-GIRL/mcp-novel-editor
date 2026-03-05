# MCP Novel Editor

AI 웹소설 플랫폼용 편집 리뷰 MCP 서버. 외부 AI(Gemini CLI, NIM Proxy, Ollama)를 호출하여 소설 에피소드에 대한 편집 리뷰를 오케스트레이션한다.

## 기능

| 도구 | 설명 |
|------|------|
| `review_episode` | 단건 에피소드 편집 리뷰 (NIM/Ollama 병렬 -> Gemini 순차) |
| `batch_review` | 여러 에피소드 일괄 리뷰 (정기 점검 P7, 아크 종료용) |
| `check_status` | 외부 AI 서비스 상태 확인 (Gemini CLI, NIM Proxy, Ollama) |

## 리뷰 파이프라인

```
Phase 1: NIM + Ollama (병렬, 플래그에 따라 선택)
    ↓ 결과를 참고 자료로 전달
Phase 2: Gemini CLI (항상, 파일시스템 접근)
    ↓ Gemini 실패 시
Fallback: NIM Proxy
```

- 각 소스의 활성화 여부는 소설 `CLAUDE.md`의 플래그로 제어
- `EDITOR_FEEDBACK_{source}.md` 파일 자동 생성
- Gemini는 파일시스템에 직접 접근하여 설정/요약 파일 참조

## 설치

```bash
cd /root/novel/mcp-novel-editor
pip install -r requirements.txt
```

## MCP 등록

`.mcp.json`에 추가:

```json
{
  "mcpServers": {
    "novel-editor": {
      "command": "python3",
      "args": ["/root/novel/mcp-novel-editor/editor_server.py"],
      "env": {
        "NOVEL_ROOT": "/root/novel"
      }
    }
  }
}
```

소설 폴더의 `.claude/settings.local.json`에 권한 추가:

```json
{
  "permissions": {
    "allow": [
      "mcp__novel-editor__*"
    ]
  },
  "enabledMcpjsonServers": ["novel-editor"]
}
```

## 사용법

Claude Code 내에서 MCP 도구로 호출:

```
# 단건 리뷰
review_episode(
    episode_file="/root/novel/no-title-013/chapters/arc-01/chapter-05.md",
    novel_dir="/root/novel/no-title-013",
    sources="auto"
)

# 일괄 리뷰 (범위)
batch_review(
    episode_files="1-10",
    novel_dir="/root/novel/no-title-013",
    sources="auto"
)

# 서비스 상태 확인
check_status()
```

### sources 파라미터

| 값 | 동작 |
|----|------|
| `"auto"` | CLAUDE.md 플래그에 따라 자동 결정 (기본) |
| `"gemini"` | Gemini만 실행 |
| `"nim"` | NIM만 실행 |
| `"nim,gemini"` | NIM + Gemini |
| `"all"` | 모든 소스 실행 |

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `NOVEL_ROOT` | `/root/novel` | 소설 프로젝트 루트 경로 |
| `NIM_PROXY_URL` | `http://localhost:8082` | NIM Proxy 주소 |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API 주소 |
| `NIM_TIMEOUT` | `600` | NIM 호출 타임아웃 (초) |
| `OLLAMA_TIMEOUT` | `900` | Ollama 호출 타임아웃 (초) |
| `GEMINI_TIMEOUT` | `600` | Gemini CLI 타임아웃 (초) |

## 외부 의존성

- **Gemini CLI**: `npm install -g @google/gemini-cli` (Google AI)
- **NIM Proxy**: [nim-proxy](https://github.com/NA-DEGEN-GIRL/nim-proxy) (NVIDIA NIM API)
- **Ollama**: https://ollama.ai (로컬 LLM)

## 관련 프로젝트

- [claude-novel-templates](https://github.com/NA-DEGEN-GIRL/claude-novel-templates) - 소설 프로젝트 템플릿
- [mcp-novel-calc](https://github.com/NA-DEGEN-GIRL/mcp-novel-calc) - 숫자/날짜 계산 MCP
- [mcp-novel-hanja](https://github.com/NA-DEGEN-GIRL/mcp-novel-hanja) - 한자 검증 MCP
- [mcp-novelai-image](https://github.com/NA-DEGEN-GIRL/mcp-novelai-image) - 삽화 생성 MCP

## License

MIT
