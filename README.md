# 원격 나래온 CLI

나래온 앱과 연동해 휴대폰·태블릿 여러 대를 선택해 조회·조작하는 Windows CLI입니다.

## 새 기능: 여러 태블릿 화면 관제 (0.4.0)

- 기기별 `--device tab1` 암호화 연결, 기존 default 연결 보존
- 최대 12개 선택 기기의 실제 화면을 PC 브라우저 격자로 주기적 갱신
- 기기별 제어권 확인 후 이미지 클릭 터치·홈/뒤로·한글 입력·스크롤
- 다른 Wi-Fi에서도 각 기기의 기존 HTTPS MCP 연결 사용; ADB·USB 불필요
- 기기별 실패 격리, 자동 재인계·변경 재시도 없음, 화면은 메모리에서만 처리

```powershell
.\naraeon.cmd --device tab1 register
.\naraeon.cmd --device tab2 register
.\naraeon.cmd monitor --devices tab1 tab2
```

**주기적 캡처이며 고FPS 영상 스트리밍은 아닙니다.** 기본은 캡처 완료 후 2초 대기입니다. Codex의 기존 PC 연결로 관제창을 보거나, 로컬 실행 가능한 Codex에서 `--device`로 기기를 지정합니다. 실제 기기의 최초 연결·접근성·허용 앱·화면 이미지 전달은 직접 승인해야 합니다.

자세한 설정·동작·검증 범위: [여러 태블릿 사용안내.md](여러%20태블릿%20사용안내.md).

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
`phone_read_screen`의 최신 revision과 노드를 기준으로 조작합니다. 최신 나래 MCP에서는 업무 종료·실패 후에도 원격 모드와 제어권을 유지하며, `narae_release_control`도 강제로 나래로 돌리지 않습니다. 사용자의 휴대폰 직접 조작으로 나래에 복귀합니다.
시간 초과가 난 변경 작업은 상태를 먼저 확인하며 자동 재시도하지 않습니다.

## 화면 캡처 · 이미지 반환

```powershell
.\naraeon.cmd screenshot
.\naraeon.cmd screenshot --output .\capture.jpg
```

최신 화면 revision을 읽고 캡처를 한 번 요청합니다. 실제 MCP 이미지 데이터를 JPEG/PNG 파일로 저장하고 `images[].path`, 크기, MIME, SHA-256, `MEDIA:` 첨부 경로를 반환합니다. base64를 기본 출력에 쏟아내지 않습니다. 기존 파일은 덮어쓰지 않으며 기본 저장 위치는 `%LOCALAPPDATA%\RemoteNaraeonCLI\captures`입니다. 실제 앱의 JPEG는 `.jpg`/`.jpeg`로 저장하세요.

**캡처 도구가 안 보이면:** 나래온 **설정 → 내장 휴대폰 조작 설정 → ‘요청한 화면 이미지를 선택한 AI 제공자에 전달 허용’**을 직접 켠 뒤 도구 목록을 다시 조회하세요. 접근성·허용 앱·현재 원격 제어권도 필요합니다. 잠금·민감 정보·보호 화면의 차단을 우회하지 않습니다. CLI가 이 권한을 자동으로 켜거나 원격 모드를 바꾸지 않습니다.

일반 `call`의 이미지 응답도 파일로 저장합니다. 호스트에서 네이티브 MCP 이미지를 직접 처리해야 할 때만 `call ... --inline-images`를 사용합니다. 스킬은 파일을 비전 도구로 열어 실제 이미지를 확인하고, 이미지 요청에는 첨부로 반환하도록 안내합니다. 캡처는 일반 이미지 파일이므로 임의로 공개 업로드하지 마세요.

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

Windows DPAPI·MCP 전송 회귀와 이미지 디코드·저장·revision·오류/재시도 방지 및 다중 기기·관제 인증·오래된 화면·실패 격리 검사를 포함합니다.
단위 검사는 가짜 자격 증명과 테스트 응답만 사용하며 실제 사용자 폰을 조작하지 않습니다. Android 캡처 종단 검증은 소유한 분리 시험 기기에서 별도로 수행합니다.

자세한 명령과 변경 사항은 [사용안내.md](사용안내.md)를 참고하세요.
