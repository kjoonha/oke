# OKe 조각 4: codex / opencode 를 worker 와 reviewer 로

2026-11-13. 처음 인터뷰 (fable 시절) 의 결정: codex, opencode 는 worker 와 reviewer 로만 지원한다. orchestrator 와 PM 은 Claude Code 여야 한다 (현황판 mod, 감시 타이머, 질문 팝업, hook 이 전부 Claude Code 기능이다). 감시는 상위의 mod 가 herdr 로 밖에서 하므로 하위 agent 종류와 상관없다.

## 고르는 법

- `~/.oke/config.json` 의 `agents`: `{"worker": "claude", "reviewer": "claude"}` 가 기본. 값은 `claude`, `codex`, `opencode`.
- 배정 파일 머리말의 `kind:` 가 config 보다 앞선다. PM 은 사용자가 intake 에서 특정 agent 를 원할 때만 적는다.
- PM 에 `kind` 가 claude 가 아니면 `oke spawn` 이 거부한다.
- children.json 기록에 `kind` 를 남긴다. 부활은 같은 종류로 한다.

## 시작 인자

| | balanced / full | conservative | reviewer |
|---|---|---|---|
| codex | `--dangerously-bypass-approvals-and-sandbox` | `-s workspace-write -a on-request --add-dir ~/.oke --add-dir <wiki>` | `-C <자기 폴더> -s workspace-write -a never` |
| opencode | `--auto` | (없음: 권한 창이 사용자에게 간다) | `--auto` |

- codex reviewer 는 sandbox 가 막는다: 어디든 읽고, 자기 OKe 폴더에만 쓴다. git 은 `git -C <cwd>` 로 부른다.
- opencode 에는 읽기 전용 모드가 없다. reviewer 는 지시로만 읽기 전용이고, PM 이 리뷰 뒤 `git status` 로 확인한다 (조각 1 부터 있는 절차).
- 모델은 넘기지 않는다. 각 CLI 의 기본 설정을 쓴다.
- 부활: codex `resume <session>`, opencode `--session <session>`.
- codex 는 늘 `-c features.hooks=false` 로 띄운다. codex 는 신뢰 안 된 hook 을 묻는 창을 herdr 가 볼 수 없는 때에 띄우고, 그때 입력 중인 prompt 글자가 그 창에 들어가 "Trust all" 을 고를 수 있다 (실제로 prompt 가 잘려 들어감). hook 을 끄면 그 창이 없다. 저장소의 `.codex` hook 도 돌지 않아 reviewer sandbox 밖으로 새는 길도 막힌다.

## 첫 지시

claude 가 아니면 plugin skill 을 못 읽는다. 첫 prompt 가 skill 파일 경로를 직접 준다: "Read the OKe worker instructions in <plugin>/skills/worker/SKILL.md ... Where they say ${CLAUDE_PLUGIN_ROOT}, use <plugin>."

## 시작 창

- 신뢰 창: claude 는 `down enter`, codex 는 `enter` (첫 항목이 이미 "Trust and continue"). 조건은 조각 1 과 같다 (OKe 가 아는 저장소, pane 이 그 안).
- codex 업데이트 제안 ("Update now / Skip"): `esc` 로 건너뛴다.
- codex hook 검토 화면과 hook 신뢰 질문: `esc` (신뢰하지 않고 닫음). hook 을 끈 뒤로는 거의 안 나오지만 남겨 둔다.
- 그 밖의 창은 사용자 몫이다: spawn 이 화면과 함께 실패한다.
- herdr 가 이미 준비됐다고 한 뒤에 뜨는 창도 있어서, codex 는 입력 줄 안내 (`? for shortcuts`) 가 보일 때까지 화면을 보며 위 창을 닫은 뒤 prompt 를 보낸다. prompt 가 멈추면 한 번 더 창을 닫고 Enter.
- 창은 차례로 최대 3개까지 처리한다.

## 세션 id

herdr 는 codex 세션 id 를 자기 SessionStart hook 으로 받는데, hook 을 껐으므로 대개 모른다. `look_at` 은 herdr 가 세션 id 를 보이면 children.json 에 적지만, 없으면 codex 부활은 새 세션으로 시작해 배정 파일과 board.md 를 다시 읽고 이어 간다.

## 안 되는 것 (알고 둠)

- codex 가 쓴 report.md 에 UTF-8 BOM 이 붙어 있었다: 머리말은 `utf-8-sig` 로 읽는다.

- API 오류 재시도 hook (`api-retry`) 과 compact 뒤 다시 읽기 hook 은 claude 전용이다. codex / opencode 가 API 오류로 멈추면 "보고 없이 쉼" 경로 (재촉, 그 뒤 `silent`) 로 올라온다.
- opencode 는 이 PC (Windows) 에서 PowerShell 실행 정책 때문에 herdr pane 에서 시작되지 않아 실제로 돌려 보지 못했다. 인자는 `opencode --help` (v2.0.26) 기준이다.
