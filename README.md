# stackchan3z

M5Stack 公式 **StackChan**（CoreS3 + シリアルサーボ首）を、家の Mac と tailnet 上の AI サーバで動かす
「顔を探して、見つけたら名前を覚えて挨拶する」ロボットのファームウェアとサーバ一式です。

*A Stack-chan (official M5Stack StackChan, CoreS3) that searches for faces, remembers people by name,
greets them in Kansai dialect via a VLM, and is reachable from anywhere on your tailnet. Firmware (Arduino) + Mac-side services.*

<p align="center"><img src="docs/stackchan.jpg" width="640" alt="stackchan3z: M5Stack StackChan with half-moon eyes"></p>

## できること

- **目**: 黒画面に白い半円の目。まばたきは半円が潰れる表現。瞳は検出した顔を追います。
- **首**: 顔が見つからなければ左右上下に首を振って探索（90秒で諦め、10分後に再開）。見つけたら首で追従。
- **顔を見つけたら**: 首を止めて写真を撮り、Mac の「脳」へ送信。
  - 知らない人 →「あんた、だれ？」と聞き、4秒録音 → Whisper で聞き取り → 名前と顔を記憶（「○○さんやな。覚えたで！」）。
  - 知っている人 → その日初めてなら時間帯に合わせた挨拶＋顔の様子の一言。以後は1時間に1回だけ「疲れてない？少し休んだら？」のような気遣い。それ以外は黙る。
- **ご主人**: 普段使う人を「ご主人」として登録（ダッシュボードの「ご主人にする」か `/owner?name=`）。
  - ご主人以外（知らない人も、名前を覚えた別の人も）が映ったら写真を保存して Mac に通知し、次にご主人が来たときに「さっき○○さんが来てたで」と口頭で報告して、その人の写真を画面に出す。
  - ご主人の写真を1日1回保存（その日最初に見つけたとき）。
  - ご主人がうたた寝していたら「風邪ひくで。ベッド行きな」と促す（両目が閉じて姿勢が崩れている、を5分以上空けて2回確認したときだけ。30分に1回まで）。
- **服装**: 顔を認識したら首を下に向けて服を撮影し、その日最初は服のコメント。以後は前回の服の写真と比べて、着替えていれば「あれ、着替えたん？」と指摘、同じなら黙る。撮り終えたら顔に戻る。
- **プロフィール収集**: ご主人と話すタイミングで（2時間に1回まで）仕事・趣味・住まい・会社の場所などを1問ずつ聞き、答えを録音→Whisper→LLMで要点抽出して `profile.json` に保存。定番の質問が埋まったら LLM が次の質問を考える。
- **ニュース雑談**: プロフィールから LLM が検索語を作り、Google ニュース RSS を検索、関係が深そうな記事を最大3件選んで要約（3時間ごと）。
  ご主人が近くにいて会話中でないときに「○○って知ってる？〜らしいんやけど」と自然に切り出し、返答を聞いて記事の範囲で答える雑談を最大3往復。見出しは画面にも表示。ダッシュボードに一覧。
- **ダッシュボード**: Mac の `http://<mac>:9002/`（tailnet なら `https://<mac>.<tailnet>.ts.net:8444/`）で、写真と発言の履歴、人物一覧（ご主人設定・削除）を一覧表示。
- **喋る**: 音声合成は Tsukasa-Speech（StyleTTS2）API か、macOS の `say`（設定不要）。
- **HTTP API**: 喋る・画面表示・首振り・写真・顔追従設定など。tailscale serve で tailnet 全体から呼べます。

## 構成

```
 StackChan (CoreS3)  ──Wi-Fi──▶  Mac (LAN)                              tailnet
 ├ 顔検出 (esp-dl, 13fps)         ├ tts_proxy.py  :9001  文→WAV          ├ Tsukasa-Speech (任意)
 ├ 首 (SCS servo, UART)           ├ brain.py      :9002  顔照合/記憶/VLM  ├ Ollama VLM (Mac Studio 等)
 ├ カメラ JPEG / マイク WAV        ├ tailnet_proxy :8080  → board:80     └ 他の端末 (client script)
 └ HTTP API :80                  └ tailscale serve :8443 → :8080
```

ボードは「撮る・録る・喋る・動く」だけを担当し、判断はすべて Mac 側の Python です。

## ハードウェア

- M5Stack **StackChan**（公式製品。CoreS3 本体、Feetech SCS0009 サーボ×2、ベース基板）
  - 首は PWM ではなく **SCS シリアルバスサーボ**（UART1 1Mbps、TX=G6/RX=G7、ID1=左右、ID2=上下）。
  - ベース基板の IO エキスパンダ（内部 I2C 0x6F）のピン0がサーボ電源。ファームが起動時に ON にします。
  - 上下サーボの可動域はマニュアルどおり 5〜85° に制限しています。
- Mac（Apple Silicon 推奨。insightface / mlx-whisper を動かします）
- 任意: Ollama が動く GPU マシン（`qwen2.5vl` / `gemma3` / `qwen3.8` など画像対応モデル）。Mac 上の Ollama でも可。

## セットアップ

### 1. ファームウェア（Arduino）

必要なもの: Arduino IDE 2 または arduino-cli、ボードパッケージ **m5stack:esp32 2.1.x**、ライブラリ **M5Unified**, **M5GFX**, **ArduinoJson 7**。
（esp-dl の顔検出モデルと esp32-camera は m5stack ボードパッケージに同梱。Feetech サーボドライバは `firmware/stackchan_web/` に同梱、MIT）

```sh
# 自分の環境値（Mac の LAN IP など）は git 管理外の config_local.h に書く
cat > firmware/stackchan_web/config_local.h <<'EOF'
#undef  CFG_TTS_URL
#define CFG_TTS_URL   "http://192.168.1.10:9001/say"
#undef  CFG_BRAIN_URL
#define CFG_BRAIN_URL "http://192.168.1.10:9002/visit"
EOF

arduino-cli compile --fqbn m5stack:esp32:m5stack_cores3 --output-dir build firmware/stackchan_web
python3 -m esptool --chip esp32s3 --port /dev/cu.usbmodem* --baud 921600 --connect-attempts 10 \
        write_flash -z 0x10000 build/stackchan_web.ino.bin
```

`arduino-cli upload` はボードが再起動中だと失敗しやすいので esptool を推奨します（初回は `--output-dir` の bootloader/partitions も 0x0/0x8000 に書く。README 末尾参照）。

### 2. Wi-Fi 設定（USB シリアル）

シリアル 115200bps に `wifi <SSID> <password>` を送ると NVS に保存されて接続します。

```sh
pip install pyserial
python3 tools/serial_cmd.py -t 25 "wifi MySSID MyPassword"
# macOS のキーチェーンからパスワードを取って送る場合
tools/provision_wifi.sh MySSID
```

注意: USB-CDC は DTR/RTS を切り替えるとリセットされます。`serial_cmd.py` はポートを開いたまま送るので安全です。
起動直後の約20秒（接続待ち）に送ったコマンドは捨てられます。

### 3. Mac 側サービス

```sh
# 脳用の Python（Apple Silicon）
pip install insightface onnxruntime opencv-python numpy mlx-whisper

# ボードの IP を指定して launchd に登録（tts: macOS say / brain: ローカル Ollama / forward）
BOARD_IP=192.168.1.50 ./server/install_services.sh

# 例: Tsukasa-Speech と、別マシンの Ollama（localhost 限定）を ssh トンネル経由で使う
BOARD_IP=192.168.1.50 TTS_API=http://100.x.y.z:8000/synthesize \
  OLLAMA_SSH_HOST=100.a.b.c VLM_URL=http://127.0.0.1:11435/api/chat VLM_MODEL=qwen3.8:27b-mxfp8 \
  PY_ML=/path/to/python3 ./server/install_services.sh
```

ログは `/tmp/com.stackchan.*.log`。初回は insightface と Whisper のモデルをダウンロードします
（Whisper は `HF_HUB_OFFLINE=1` で動かします。この環境では Hugging Face のオンライン確認が数分固まるため。未取得なら自動で一度だけオンライン取得します）。

### 4. tailnet から使う（任意）

```sh
tools/mac_tailscale.sh expose      # tailscale serve --https=8443 → 127.0.0.1:8080 → board
tailscale serve --bg --https=8444 http://127.0.0.1:9002   # ダッシュボードも tailnet に公開
STACKCHAN_URL=https://<mac>.<tailnet>.ts.net:8443 python3 tools/stackchan_client.py status
```

ボードから tailnet 上のサーバへ出る方向は `server/tailnet_proxy.py <port> <host> <port>` で中継するか、
`sudo tools/mac_tailscale.sh nat` で Mac を NAT ゲートウェイにします（ボード側 `gw <mac-ip>`）。

## HTTP API（ボード :80）

| endpoint | 内容 |
|---|---|
| `/api/status` | 状態 JSON（IP, RSSI, カメラ, 顔, 首の角度など） |
| `/api/say?text=...&emotion=happy` | 喋る（tts_proxy 経由） |
| `/api/display?text=...&size=1..3&ms=4000` | 画面に文字（日本語可、`\n` 改行。size1 は下段バー、2〜3 は中央） |
| `/api/display?image=URL&ms=6000` | JPEG（QVGA 推奨）を画面に表示。`/photos/...` のような相対パスは脳サービスから取得 |
| `/api/head?gesture=nod\|shake\|center&n=2` | 首のジェスチャー |
| `/api/head?pan=60&tilt=100&speed=3` | 首の絶対角（pan 10〜170、tilt 62〜118 = pitch 5〜85°） |
| `/api/camera.jpg?q=80` | QVGA JPEG |
| `/api/track?on=1&auto=1&head=1&mirror=0&pansign=-1&tiltsign=-1&search=1` | 顔追従・自動訪問・首追従・方向・探索の設定と状態 |
| `/api/comment` | 今すぐ訪問（写真→顔認識→挨拶 or 名前確認）を実行 |
| `/api/dialog?path=/chat/session?sid=..` | 脳が用意した会話をボード側で開始（喋る→聞く→送る を返答が続く限り繰り返す） |
| `/api/fetch?url=...` | ボードから URL を GET |

`tools/stackchan_client.py` が全部を包んでいます（標準ライブラリのみ）。

```sh
python3 tools/stackchan_client.py say "こんにちはー"
python3 tools/stackchan_client.py display "こんにちは\n元気？" --size 2
python3 tools/stackchan_client.py head nod --n 3
python3 tools/stackchan_client.py photo shot.jpg
```

## 脳サービス API（Mac :9002）

| endpoint | 内容 |
|---|---|
| `POST /visit` (image/jpeg) | 顔照合 → `{known, name, say, ask, face_id}` |
| `POST /learn?face_id=..` (audio/wav) | 録音 → Whisper → LLM で名前抽出 → 記憶 → `{name, say}` |
| `GET /people` / `GET /forget?name=..` / `GET /owner?name=..` | 記憶した人の一覧 / 削除 / ご主人に設定 |
| `POST /clothes?name=..` (image/jpeg) | 服装の写真 → その日初回はコメント、2回目以降は前回と比較して変化だけ指摘 → `{say, changed}` |
| `POST /answer?key=..&q=..` (audio/wav) | プロフィール質問への答え → 要点を `profile.json` に保存 → `{say}` |
| `GET /profile` / `GET /topics` / `GET /topics/refresh` | プロフィール / 話題一覧 / 今すぐ検索 |
| `POST /chat?sid=..` (audio/wav) / `GET /chat/session?sid=..` | ニュース雑談の次の一手（返答→LLM→`{say, listen?}`） / 脳から始めた雑談の切り出し取得 |
| `GET /` / `GET /events.json` / `GET /photos/<file>` | ダッシュボード / 履歴 JSON / 保存写真 |

記憶は `~/Library/Application Support/stackchan/faces.json`（顔特徴量・ご主人フラグ・挨拶した日・様子見の時刻・服の写真）、
プロフィールは `profile.json`、話題は `topics.json`、履歴は `events.jsonl`、写真は `photos/`。直近の録音（`last_learn.wav`）も残ります。

`/visit` の応答には、ボードへの指示として `listen`（質問を喋って録音し、そのURLへ送る。返答にまた `listen` があれば続ける）、
`display`（画面に掲示する文）、`show`（表示する写真）、`clothes`（首を下げて服を撮る）、`recheck`（何秒後にもう一度見るか）が含まれます。

## シリアルコマンド

`wifi <ssid> <pass>` `scan` `say [text]` `greet <text>` `tts <url>` `brain <url>` `target <url>` `gw <ip>|off`
`head nod|shake|center|<pan> <tilt>` `track on|off|mirror` `follow on|off|flippan|fliptilt` `search [off]`
`comment` `learn <face_id>` `ping`（サーボ応答と生位置）`vmen on|off`（サーボ電源）`i2cint`（内部 I2C スキャン）
`camtest` `status` `clear`（NVS 消去）`reboot`

## 調整ポイント

- `firmware/stackchan_web/config.h`: 首の方向符号、探索の時間、再確認間隔、音量、挨拶文。
- `firmware/stackchan_web/face.h`: 目の形・まばたき・視線。
- `server/brain.py`: 挨拶/様子見/居眠りのプロンプト、`CHECKIN_INTERVAL_S`（様子見の間隔）、`SIM_THRESHOLD`（同一人物判定）、
  `REPORT_COOLDOWN_S`（ご主人以外の報告間隔）、`NAG_INTERVAL_S`（居眠り注意の間隔）、`PROFILE_QUESTIONS`（定番の質問）。
  環境変数 `STACKCHAN_OWNER_PHOTO_INTERVAL`（ご主人の写真間隔、秒）、`STACKCHAN_ASK_INTERVAL`（質問の間隔）、`STACKCHAN_TOPIC_INTERVAL`（話題更新の間隔）、
  `STACKCHAN_BOARD_URL`（話題を掲示するボードの URL。`install_services.sh` が BOARD_IP から設定）。
- `server/tts_proxy.py`: TTS バックエンド。`STACKCHAN_SAY_VOICE` で `say` の声を変更。

## ハマりどころ

- 起動時の `esp_camera_init` は1回目が「i2c driver install error」で失敗します。ファームはリトライして成功させています。
- esp-dl の推論中にカメラのフレームを掴んだままにすると `cam_hal` のログでドライバタスクがスタック溢れします。フレームは即コピーして返却し、`cam_hal` のログを止めています。
- CoreS3 の Port A（G1/G2）はカメラの XCLK と共有なので、カメラ動作中は使えません。
- サーボが全く動かないときは `ping` で応答を、`vmen on` で電源を確認してください。
- 首が顔から逃げる方向に動くときは `follow flippan` / `follow fliptilt`（または config.h の符号）。3〜4ステップ連続で悪化すると自動反転もします。

## 初回フラッシュ（bootloader ごと）

```sh
python3 -m esptool --chip esp32s3 --port /dev/cu.usbmodem* --baud 921600 write_flash -z \
  0x0 build/stackchan_web.ino.bootloader.bin 0x8000 build/stackchan_web.ino.partitions.bin \
  0xe000 ~/Library/Arduino15/packages/m5stack/hardware/esp32/2.1.3/tools/partitions/boot_app0.bin \
  0x10000 build/stackchan_web.ino.bin
```

## ライセンス

MIT（`LICENSE`）。同梱の Feetech サーボドライバは MIT（`firmware/stackchan_web/LICENSE.FTServo`）。
顔検出は Espressif esp-dl、カメラは esp32-camera（いずれも m5stack ボードパッケージ同梱）。
