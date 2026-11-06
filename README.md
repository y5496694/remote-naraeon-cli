# 원격 나래온 CLI

나래온 앱과 연동하여 핸드폰을 컨트롤 할 수 있는 cli.

Windows에서 나래온 MCP 서버에 접속해 휴대폰 연결 상태, 화면, 앱과 도구를 조회·조작합니다.
저장된 연결정보를 자동으로 불러오며 **PC 보안 PIN이나 잠금 해제 절차는 없습니다.**

## 준비

- Windows 10/11
- Python 3.10 이상. 별도 Python 패키지는 필요하지 않습니다.
- 나래온 Android 앱과 사용자가 승인한 원격 연결
- 실제 나래온 MCP HTTPS 주소와 Bearer 토큰 또는 이 PC에 등록된 나래온 연결정보

## 다운로드와 실행

[최신 릴리스](https://github.com/y5496694/remote-naraeon-cli/releases/latest)에서 ZIP을 받아 압축을 풉니다.
소스를 Git으로 내려받아도 같은 명령을 사용할 수 있습니다.

```powershell
.\naraeon.cmd status
.\naraeon.cmd info
.\naraeon.cmd phone-status
.\naraeon.cmd tools
```

이 PC에 기존 나래온 연결정보가 있으면 등록 없이 바로 불러옵니다.
기존 정보가 없는 PC에서는 다음 명령으로 주소와 Bearer 토큰을 숨김 입력합니다.

```powershell
.\naraeon.cmd register
```

기존 연결정보를 CLI 전용 저장소에도 복사하려면 `연결정보 등록.cmd`를 실행하거나:

```powershell
.\naraeon.cmd register --import-existing
```

연결정보를 교체하려면 `register --replace` 또는 `register --import-existing --replace`를 사용합니다.
서버의 연결 승인 PIN을 토큰으로 교환하는 기능은 현재 포함되어 있지 않습니다.

## 도구 사용

현재 서버에서 제공하는 이름과 인수는 `tools`로 확인합니다.

```powershell
.\naraeon.cmd tools --name phone_act
.\naraeon.cmd call phone_connection_status
.\naraeon.cmd call 도구이름 --args-file 인수.json
```

휴대폰 조작 전 연결 상태와 허용 앱을 확인하고, `narae_take_control`로 제어권을 인계받습니다.
`phone_read_screen`의 최신 revision과 노드를 기준으로 조작하며, 완료 후 `narae_release_control`로 반환합니다.
시간 초과가 난 변경 작업은 상태를 먼저 확인하며 자동 재시도하지 않습니다.

## 연결정보 저장

CLI 전용 저장 파일은 `%LOCALAPPDATA%\RemoteNaraeonCLI\connection-v2.dpapi`이며
Windows 현재 사용자 DPAPI로 암호화합니다. 저장 파일이 없으면 기존
`NARAE_MCP_URL`/`NARAE_MCP_TOKEN` 환경변수 또는
`$CODEX_HOME/secrets/narae-phone.dpapi`(기본 `~/.codex`)를 사용합니다.

실제 주소·토큰·자격 증명 파일은 이 저장소와 릴리스 ZIP에 포함되지 않습니다.
각 PC에서 본인의 연결정보를 사용합니다. 나래온 서버의 인증과 토큰 만료 정책은 적용됩니다.

## Codex 플러그인

`plugin.json`과 `skills/remote-naraeon`을 포함한 데스크톱 로컬 실행용 패키지입니다.
지원하는 호스트의 ZIP 업로드 또는 로컬 플러그인 설치 기능을 이용합니다.
설치 후 스킬이 로컬 Python CLI를 호출하는 구조이며, 원격 MCP OAuth 등록 양식을 사용하지 않습니다.
플러그인 설치 후의 동작은 사용하는 호스트에서 확인해야 합니다.

## 테스트

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

Windows DPAPI 저장·불러오기, 입력 없는 기존 연결정보 사용, 인증 오류, 재시도 방지 등 13개 검사를 포함합니다.
실제 MCP 서버 초기화 응답도 확인했습니다. 테스트에서는 휴대폰을 조작하거나 메시지를 발송하지 않습니다.

자세한 명령과 변경 사항은 [사용안내.md](사용안내.md)를 참고하세요.
