<h1 align="center">OKe</h1>

<p align="center">
  <a href="README.md">English</a> | <b>한국어</b>
</p>

<p align="center">
  <img src="docs/oke-band.svg" alt="OKe 캐릭터들이 일하는 모습: disco, slime, mushroom theme 의 orchestrator, PM, worker, reviewer">
</p>

<p align="center">
  Claude Code 플러그인. 사용자는 orchestrator 한 명과만 대화한다.<br>
  orchestrator 가 저장소마다 PM 을 두고, PM 이 worker 와 reviewer 를 herdr pane 에 띄워 일을 시킨다.<br>
  질문과 승인은 orchestrator 창의 팝업으로 올라온다.
</p>

agent 에게 가는 메시지는 그 agent 의 inbox (`$OKE_HOME/inbox/<agent>/`) 에서 기다리다가 그 agent 의 턴이 끝나는 즉시 입력된다. 백그라운드 subagent 나 작업이 아직 돌아도 그렇다. 그래서 한 job 의 리뷰를 돌리는 PM 도 다음 job 을 바로 받는다.

## 필요한 것

- Claude Code 2.1.322 이상 (mod 를 지원하는 버전)
- herdr 0.9.5 이상 (`herdr agent start / prompt / wait` 가 있어야 한다), 또는 없으면 `claude --bg` 백그라운드 세션
- Python 3.6 이상 (표준 라이브러리만 쓴다), git

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
mkdir -p ~/.claude/plugins/marketplaces/oke && tar xzf oke.tar.gz -C ~/.claude/plugins/marketplaces/oke
claude plugin marketplace add ~/.claude/plugins/marketplaces/oke
claude plugin install oke@oke
```

GitHub 으로 받은 marketplace 와 같은 자리다. 플러그인은 이 폴더에서 읽히므로 지우지 않는다 (`claude plugin marketplace remove oke` 가 함께 지운다). 새 버전은 같은 폴더에 다시 풀고 `claude plugin marketplace update oke` 를 부른다.

## 쓰기

herdr pane 또는 일반 터미널에서 Claude Code 를 켜고 `/oke:start`. 그 세션이 orchestrator 가 된다. 하고 싶은 일을 말하면 된다. 세션이 끊겼으면 `/oke:resume`.

`oke status` (와 `oke board`) 는 어느 터미널에서나 된다: orchestrator 가 고른 runtime 을 읽는다. 그 runtime 의 터미널 밖 (예: OKe 가 herdr 에서 도는데 일반 셸) 에서는 보여 주기만 하고 아무것도 띄우지 못한다.

검사: 계획에는 번호 붙은 수용 기준 (`## 수용 기준`) 과 검사 명령 하나 (`## 검증 명령`, 백틱 안) 가 있다. 일을 시작하기 전에 `oke baseline <job>` 이 손대지 않은 checkout 에서 명령을 돌려 원래 깨진 것을 저장한다. worker 는 `oke check <job>` 으로 검사한다: OKe 가 명령을 돌리고, baseline 에 없던 실패 줄만 새 실패로 친다. 결과는 OKe 가 job 장부에 적고 줄마다 서명한다. 서명 키 (`$OKE_HOME/ledger.key`) 는 agent 가 읽을 수 없고, 손으로 적거나 복사한 줄은 서명이 맞지 않아 세지 않는다. `oke job merge` 는 리뷰가 계획의 현재 `criteria_rev` (수용 기준과 명령의 해시) 를 적었고, 장부에 리뷰한 커밋에서 통과한 `oke check` 가 있을 때만 합친다. 합친 뒤 `oke job verify <job>` 은 합친 코드가 검사를 통과한 코드와 같으면 돌리지 않고 통과로 적고, 검사 뒤 다른 merge 가 main 을 바꿨으면 바뀐 파일 목록을 보여 주고 PM 이 정한다: 그 파일이 결과를 바꿀 수 없으면 (예: 문서) `--skip "<이유>"` 로 이유와 파일 목록을 장부에 남기고, 아니면 `--run` 으로 main 에서 명령을 돌려 새 실패가 있을 때 merge 를 되돌린다.

## 예약 실행: `/oke:automate`

아무 Claude Code 세션에서 `/oke:automate` 를 부르고 무엇을 얼마나 자주 돌릴지 말한다. Claude 가 프롬프트, 폴더, 주기, 권한 모드, (원하면) 모델을 묻고, 설정을 보여 준 뒤 등록한다: Linux 와 macOS 는 `crontab`, Windows 는 작업 스케줄러 (`OKe` 폴더). 정한 시각마다 그 폴더에서 저장된 프롬프트로 백그라운드 세션 (`claude --bg`, 이름 `auto-<name>-<MMDD-HHMMSS>`) 이 뜬다. `claude attach <session>` 으로 보거나 `claude agents` 로 목록을 본다.

- 주기 (이 컴퓨터의 시각): `every 30m`, `every 2h`, `daily 09:00`, `weekdays 09:00`, `weekly mon,thu 09:00`, Windows 밖에서는 `cron <다섯 칸>`.
- 폴더는 Claude Code 가 이미 신뢰하는 곳이어야 한다: 백그라운드 세션은 신뢰 창에 답하지 못한다.
- 예약 세션의 권한 창에는 아무도 답하지 않는다. 사용자 기본 권한 모드면 첫 권한 창에서 누가 attach 할 때까지 기다린다. 같은 automation 의 이전 실행이 아직 일하거나 기다리는 동안 다음 실행은 건너뛴다.
- 그 시각에 컴퓨터가 켜져 있어야 한다. Windows 는 사용자가 로그인해 있어야 한다 (배터리로 돌 때도 실행된다). 실행은 등록할 때 셸의 PATH 와 locale 을 받는다.
- automation 하나는 폴더 `$OKE_HOME/automations/<name>/` 하나다 (`automation.json`, `prompt.md`, 실행마다 (손으로 돌린 것도) 한 줄씩 쌓이는 `run.log`). `oke automate list`, `oke automate run <name>` (지금 한 번), `oke automate remove <name>` 은 셸에서도 된다.

## 설정: `~/.oke/config.json`

없어도 된다. 있으면 아래 값만 덮어쓴다.

OKe 는 파일을 `~/.oke` 에 둔다. 다른 폴더를 쓰려면 `~/.claude/settings.json` 의 `env` 에 `OKE_HOME` 을 넣는다 (`"env": {"OKE_HOME": "D:/oke"}`). 그러면 OKe 의 모든 agent 가 그 값을 받는다.

```json
{
  "autonomy": "balanced",
  "wiki_path": "~/.oke/wiki",
  "models": {"pm": "opus", "worker": "opus", "reviewer": "opus"},
  "agents": {"worker": "claude", "reviewer": "claude"},
  "interview_threshold": 0.2,
  "interview_max_rounds": 10,
  "lean_launch": true,
  "effort": {"pm": "high", "worker": "medium", "reviewer": "high"},
  "theme": "disco"
}
```

- `theme`: 대화창 위 띠에 나오는 캐릭터. 역할마다 모양이나 색이 다르다.
  - `disco` (기본): 헤드폰 낀 블롭. orchestrator 보라, PM 파랑, worker 초록, reviewer 주황.
  - `slime`: 통통 뛰는 슬라임. orchestrator 초록, PM 파랑, worker 분홍, reviewer 노랑.
  - `mushroom`: 통통 뛰는 버섯. orchestrator 보라, PM 파랑, worker 주황, reviewer 초록.
  - `cute-dog` (귀가 처진 강아지), `cute-cat` (고양이): 꼬리를 흔들고 귀를 쫑긋하고 눈을 깜빡이며, 잘 때는 몸을 만다. 역할마다 털색이 다르다.
  - disco 색 (orchestrator 보라, PM 파랑, worker 초록, reviewer 주황): `ghost`, `robot`, `frog`, `penguin`, `bunny`, `bear`, `duck`, `octopus`, `alien`, `cactus`, `jellyfish`, `fox`, `panda`, `hamster`, `owl`, `whale`, `dino`, `star`, `pixel-heart`, `coffee`, `onigiri`, `axolotl`, `bee`, `snail`, `chick`, `sakura`.
  - `claude`: 팔과 다리 네 개가 달린 네모난 캐릭터. 일할 때 팔을 번쩍 든다. orchestrator Claude 주황, PM 파랑, worker 초록, reviewer 크림.
  - 색 변형: `pastel` (토끼), `candy` (하트), `ocean` (고래), `autumn` (여우), `midnight` (부엉이).
  - 컬러 스킴 (역할 색을 그 스킴의 팔레트에서 가져온다): `tokyo-night`, `catppuccin` (mocha), `github` (dark), `dracula`, `nord`, `gruvbox`, `solarized`, `one-dark`, `monokai`, `rose-pine`, `everforest`, `kanagawa`, `ayu`, `material`, `synthwave-84`.
  - 모르는 이름이면 `disco`.

- `autonomy`: `conservative` (worker 가 명령마다 승인을 묻는다), `balanced` (기본), `full`. `full` 이면 orchestrator 가 계획과 merge 를 묻지 않고 승인하고, 죽은 (`gone`) PM 이나 자식을 한 번은 묻지 않고 다시 띄운다 (`oke revive <name> --auto`); 한 줄로 알려 준다 (PM 은 다시 띄운 자식을 자기 board 에 적는다). 그 뒤 아무 보고 없이 또 죽으면 묻는다 (resume / hold / cancel). hold 한 job 은 다시 띄우지 않는다. `conservative` 와 `balanced` 는 늘 묻는다.
- `lean_launch`: PM / worker / reviewer 를 사용자 plugin, hook, MCP 서버 없이 띄운다 (`--setting-sources project,local --strict-mcp-config`). 사용자 `settings.json` 에서 herdr hook 과 `env` 만 `~/.oke/lean-settings.json` 으로 옮긴다. 시작 context 가 약 76k 에서 40k token 으로 준다. 도구도 역할별 핵심 도구만 켠다 (`--tools`, 시작 context 약 15k token). PM 이 job 마다 assignment header 의 `tools:`, `mcp:`, `plugins:` 로 필요한 것만 더 켠다 (`oke.py extras` 가 목록을 보여준다). 자식에게 개인 설정 전부가 필요하면 `false`.
- `models`: 역할 (`pm`, `worker`, `reviewer`) 마다 쓸 model. 기본 `opus`.
- `effort`: 역할 (`pm`, `worker`, `reviewer`) 마다 effort 수준: `low`, `medium`, `high`, `xhigh`, `max`. `lean_launch` 이면 빠진 역할은 사용자 설정의 `effortLevel` 이 아니라 Claude Code 기본 effort 로 돈다. `lean_launch` 가 `false` 면 자식이 사용자 설정을 읽는다.

- `interview_threshold`, `interview_max_rounds`: job 을 받을 때 orchestrator 가 하는 인터뷰. 목표, 제약, 성공 기준을 0~1 로 매겨 모호도(`1 - 0.4 목표 - 0.3 제약 - 0.3 기준`)가 이 값 이하가 될 때까지, 가장 흐린 부분만 한 번에 하나씩 묻는다. 사용자가 "이만 계획" 을 고르거나 최대 횟수에 닿으면 끝난다.

설치 뒤 확인 순서는 `docs/work-checklist.md`.
