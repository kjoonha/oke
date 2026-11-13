# OKe

Claude Code 플러그인. 사용자는 orchestrator 한 명과만 대화한다. orchestrator 가 저장소마다 PM 을 두고, PM 이 worker 와 reviewer 를 herdr pane 에 띄워 일을 시킨다. 질문과 승인은 orchestrator 창의 팝업으로 올라온다.

## 필요한 것

- Claude Code 2.1.322 이상 (mod 를 지원하는 버전)
- herdr 0.9.5 이상 (`herdr agent start / prompt / wait` 가 있어야 한다)
- Python 3.6 이상 (표준 라이브러리만 쓴다), git
- 선택: codex 또는 opencode (worker / reviewer 로 쓸 때)

## 설치 (인터넷 되는 곳)

```
claude plugin marketplace add kjoonha/oke
claude plugin install oke@oke
```

업데이트: `claude plugin marketplace update oke` 뒤 `claude plugin update oke@oke`.

## 설치 (인터넷 없는 곳, ftp 반입)

인터넷 되는 곳에서 묶음 하나를 만든다. 설치물은 이 폴더뿐이다 (패키지, daemon, DB 없음). `core.autocrlf=false` 는 Windows 에서 만들어도 줄 끝을 LF 로 둔다.

```
git -c core.autocrlf=false archive --format=tar.gz -o oke.tar.gz HEAD .claude-plugin oke
```

반입한 곳에서 (Linux, tcsh 도 같다):

```
mkdir -p ~/tools/oke && tar xzf oke.tar.gz -C ~/tools/oke
claude plugin marketplace add ~/tools/oke
claude plugin install oke@oke
```

플러그인은 그 폴더에서 바로 읽힌다. 새 버전은 같은 폴더에 다시 풀고 `claude plugin marketplace update oke` 를 부른다.

## 쓰기

herdr pane 에서 Claude Code 를 켜고 `/oke:start`. 그 세션이 orchestrator 가 된다. 하고 싶은 일을 말하면 된다. 세션이 끊겼으면 `/oke:resume`.

## 설정: `~/.oke/config.json`

없어도 된다. 있으면 아래 값만 덮어쓴다.

```json
{
  "autonomy": "balanced",
  "wiki_path": "~/.oke/wiki",
  "models": {"pm": "opus", "worker": "sonnet", "reviewer": "sonnet"},
  "agents": {"worker": "claude", "reviewer": "claude"},
  "interview_threshold": 0.2,
  "interview_max_rounds": 10
}
```

- `autonomy`: `conservative` (worker 가 명령마다 승인을 묻는다), `balanced` (기본), `full`.
- `agents`: worker / reviewer 를 띄울 CLI. `claude`, `codex`, `opencode`. orchestrator 와 PM 은 늘 claude 다. 제약은 `docs/superpowers/specs/2026-11-13-oke-slice4-design.md` 의 "안 되는 것".

- `interview_threshold`, `interview_max_rounds`: job 을 받을 때 orchestrator 가 하는 인터뷰. 목표, 제약, 성공 기준을 0~1 로 매겨 모호도(`1 - 0.4 목표 - 0.3 제약 - 0.3 기준`)가 이 값 이하가 될 때까지, 가장 흐린 부분만 한 번에 하나씩 묻는다. 사용자가 "이만 계획" 을 고르거나 최대 횟수에 닿으면 끝난다.

설치 뒤 확인 순서는 `docs/work-checklist.md`.
